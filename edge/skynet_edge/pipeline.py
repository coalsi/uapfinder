"""Per-frame processing: detection -> tracking -> filters -> kinematics -> reports.

This is the single-threaded core of the node. It takes one luma plane, its
capture time, the current attitude, the node position and the ADS-B aircraft
list, and returns which telemetry samples to send. Keeping it free of threads,
sockets and clocks makes it deterministic, so the same object runs the live
node, ``replay``, the bench and the integration test.

Report policy (see ``AnomalyConfig``): a confirmed track that no
false-positive filter explains is reported once it is ``report_min_age_s``
old. From then on one sample per ``1/report_hz`` is emitted, starting up to
``report_backfill_s`` in the past so the server also gets the beginning of
the track. If a filter later explains the track (e.g. an ADS-B position
arrives), reporting stops.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from .adsb import Aircraft
from .config import EdgeConfig
from .detection import Detector, make_detector
from .filters import FilterBank, FilterContext
from .flow import CloudFlow
from .geometry import Attitude, CameraModel
from .kinematics import anomaly_score, fit_kinematics, mean_angular_speed
from .skymask import SkyMask
from .tracking import Track, TrackSample, Tracker

log = logging.getLogger(__name__)

_SPEED_WINDOW_NS = 250_000_000  # angular speed of a report: mean over the preceding 0.25 s


@dataclass
class Report:
    """One telemetry sample to send for a track."""

    track_id: str
    t_ns: int
    az_deg: float
    el_deg: float
    angular_velocity_deg_s: float
    relative_intensity: float
    anomaly_score: float


@dataclass
class FrameResult:
    t_ns: int
    blobs: int = 0
    disturbed: bool = False
    active: List[Track] = field(default_factory=list)
    reportable: List[Track] = field(default_factory=list)
    reports: List[Report] = field(default_factory=list)
    ended: List[Track] = field(default_factory=list)


class StageTimer:
    """Exponentially averaged and peak wall time per pipeline stage (ms)."""

    def __init__(self, alpha: float = 0.02):
        self.alpha = alpha
        self.mean_ms: Dict[str, float] = {}
        self.max_ms: Dict[str, float] = {}
        self.samples: Dict[str, List[float]] = {}
        self.keep_samples = False

    def add(self, stage: str, ms: float) -> None:
        prev = self.mean_ms.get(stage)
        self.mean_ms[stage] = ms if prev is None else prev + self.alpha * (ms - prev)
        self.max_ms[stage] = max(self.max_ms.get(stage, 0.0), ms)
        if self.keep_samples:
            self.samples.setdefault(stage, []).append(ms)

    def snapshot(self) -> Dict[str, float]:
        return {k: round(v, 3) for k, v in self.mean_ms.items()}


class Pipeline:
    def __init__(self, cfg: EdgeConfig, cam: CameraModel, detector: Optional[Detector] = None,
                 track_id_prefix: Optional[str] = None):
        self.cfg = cfg
        self.cam = cam
        fps = cfg.camera.fps
        self.detector = detector or make_detector(cfg.detection, cam.width, cam.height, fps)
        self.tracker = Tracker(cfg.tracking, fps, track_id_prefix)
        self.flow = CloudFlow(cam.width, cam.height, cfg.tracking.dense_flow_every, cfg.tracking.dense_flow_width)
        self.filters = FilterBank(cfg.filters, fps)
        self.sky = SkyMask(cfg.detection.sky_mask, cam)
        self.timer = StageTimer()
        self.labels: Counter = Counter()  # tracks explained by each filter (counted once per track)
        self.reported_tracks = 0
        self.frames = 0
        self._prev_y: Optional[np.ndarray] = None
        self._period_ns = int(round(1e9 / cfg.server.report_hz))
        self._analysis_ns = int(round(1e9 / cfg.tracking.analysis_hz))
        self._half_frame_ns = int(0.5e9 / fps)

    def process(self, y: np.ndarray, t_ns: int, att: Attitude,
                observer: Optional[Tuple[float, float, float]] = None,
                aircraft: Sequence[Aircraft] = ()) -> FrameResult:
        """Run every stage on one frame. ``y`` must stay valid until the next call (LK uses it)."""
        self.frames += 1
        timer = self.timer
        res = FrameResult(t_ns)

        t0 = time.perf_counter()
        if self.sky.update(att):
            self.detector.set_sky_mask(self.sky.mask)
        det = self.detector.detect(y)
        t1 = time.perf_counter()
        res.blobs, res.disturbed = len(det.blobs), det.disturbed

        out = self.tracker.update(t_ns, det.blobs, lambda uv: self.cam.pixels_to_azel(uv, att), self._prev_y, y)
        t2 = time.perf_counter()
        timer.add("track", (t2 - t1) * 1e3)
        self.detector.update_background(y, self._protected_boxes(t_ns))
        t2b = time.perf_counter()
        timer.add("detect", (t1 - t0 + t2b - t2) * 1e3)  # detection includes the background update
        t2 = t2b

        self.flow.update(y, t_ns)
        t3 = time.perf_counter()
        timer.add("flow", (t3 - t2) * 1e3)

        ctx: Optional[FilterContext] = None
        for tr in out.active:
            if not tr.analyzed_ns or t_ns - tr.analyzed_ns >= self._analysis_ns - self._half_frame_ns:
                if ctx is None:
                    ctx = FilterContext(t_ns, observer, aircraft, self.flow)
                    ctx.prepare()
                self._analyze(tr, ctx)
                tr.analyzed_ns = t_ns
            verdict = tr.filtered_by()
            if verdict is None and self._reportable(tr):
                res.reportable.append(tr)
                if not tr.reported:
                    tr.reported = True
                    self.reported_tracks += 1
                    log.info("track %s reportable (age %.2f s, anomaly %.2f)", tr.id, tr.age_s, tr.anomaly_score)
                tr.flags.discard("suppressed")
                res.reports.extend(self._due_reports(tr, t_ns))
            elif tr.reported and verdict is not None and "suppressed" not in tr.flags:
                tr.flags.add("suppressed")
                log.info("track %s no longer reported: %s", tr.id, verdict.label)
        t4 = time.perf_counter()
        timer.add("filter", (t4 - t3) * 1e3)
        timer.add("total", (t4 - t0) * 1e3)

        res.active = out.active
        res.ended = out.ended
        for tr in out.ended:
            label = tr.filtered_by()
            log.debug("track %s ended after %.2f s (%s)", tr.id, tr.age_s, label.label if label else "unexplained")
        self._prev_y = y
        return res

    # --- helpers --------------------------------------------------------------

    def _protected_boxes(self, t_ns: int) -> List[Tuple[int, int, int, int]]:
        """Boxes of this frame's detections that belong to young or moving tracks."""
        d = self.cfg.detection
        young_ns = int(d.protect_young_s * 1e9)
        boxes = []
        for tr in self.tracker.tracks:
            if not tr.samples or tr.last.t_ns != t_ns or tr.last.source != "det":
                continue
            speed = float(np.hypot(tr.kf.x[2], tr.kf.x[3]))
            if t_ns - tr.first_ns < young_ns or speed >= d.protect_min_speed_px_s:
                boxes.append(tr.last.bbox)
        return boxes

    def _analyze(self, tr: Track, ctx: FilterContext) -> None:
        """Kinematics, anomaly score and false-positive filters (at ``tracking.analysis_hz``)."""
        self._update_kinematics(tr)
        before = tr.filtered_by()
        verdict = self.filters.evaluate(tr, ctx)
        if verdict is not None and (before is None or before.label != verdict.label):
            if verdict.label not in tr.flags:
                self.labels[verdict.label] += 1
                tr.flags.add(verdict.label)
            log.info("track %s %s: %s", tr.id, verdict.label, verdict.reason)

    def _update_kinematics(self, tr: Track) -> None:
        a = self.cfg.anomaly
        k = fit_kinematics(tr.recent(a.window_s), a.min_samples, a.min_speed_for_turn_deg_s)
        tr.kinematics = k
        if k is None:
            return
        tr.anomaly_score = anomaly_score(k, a)
        tr.peak_anomaly = max(tr.peak_anomaly, tr.anomaly_score)
        if k.turn_rate_deg_s >= a.maneuver_turn_rate_deg_s:
            tr.flags.add("maneuvered")

    def _reportable(self, tr: Track) -> bool:
        a = self.cfg.anomaly
        if tr.age_s < a.report_min_age_s:
            return False
        return a.report_unidentified or tr.peak_anomaly >= a.min_anomaly_score

    def _due_reports(self, tr: Track, now_ns: int) -> Iterator[Report]:
        """Samples of ``tr`` to send now: one per report period since the last one sent."""
        if tr.last_report_ns:
            due = tr.last_report_ns + self._period_ns
        else:
            due = max(tr.first_ns, now_ns - int(self.cfg.anomaly.report_backfill_s * 1e9))
        horizon = due - self._half_frame_ns - _SPEED_WINDOW_NS
        samples: List[TrackSample] = []
        for s in reversed(tr.samples):  # only the tail is needed; the history can be long
            samples.append(s)
            if s.t_ns < horizon:
                break
        samples.reverse()
        for i, s in enumerate(samples):
            if s.t_ns < due - self._half_frame_ns:
                continue
            yield self._report(tr, samples, i)
            # Keep the nominal cadence unless there was a gap longer than a period.
            due = (due if s.t_ns - due < self._period_ns else s.t_ns) + self._period_ns
            tr.last_report_ns = due - self._period_ns

    def _report(self, tr: Track, samples: List[TrackSample], i: int) -> Report:
        s = samples[i]
        j = i
        while j > 0 and s.t_ns - samples[j - 1].t_ns <= _SPEED_WINDOW_NS:
            j -= 1
        speed = mean_angular_speed(samples[j:i + 1]) if i > j else None
        if speed is None:
            speed = tr.kinematics.speed_deg_s if tr.kinematics is not None else 0.0
        return Report(tr.id, s.t_ns, s.az, s.el, float(speed), float(min(1.0, max(0.0, s.relative_intensity))),
                      round(float(tr.anomaly_score), 3))
