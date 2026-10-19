"""False-positive filters: label tracks that are explainable so they are not reported.

Each filter looks at a confirmed track's history and returns a ``Verdict``
(label + human-readable reason) or None. A labelled track is not sent as
telemetry; the label and reason are logged and kept with any event clip so
an operator can audit what was suppressed and why.

* ``slow``: end-to-end angular speed below a threshold. Stars and planets
  move 0.0042 deg/s (sidereal rate), stationary lights not at all.
* ``cloud``: large, diffuse, low-contrast structure that is slow or moves
  with the coarse dense-flow (wind) field.
* ``aircraft``: matches an ADS-B aircraft dead-reckoned to each sample time
  within an angular gate over most recent samples.
* ``bird``: pixel-expansion profiling for objects close to the lens: large
  apparent size plus either a periodic area oscillation (wingbeat 2-25 Hz,
  found with an FFT of the detrended ln(area) series) or both a fast change
  of ln(area) (approach or retreat) and defocus (low SNR per pixel). Size or
  defocus alone is not enough: a large, soft object far away is exactly what
  must not be hidden.
* ``satellite`` / ``meteor`` (disabled by default): steady great-circle
  motion with steady brightness, or a very fast short straight streak.
  Off by default because one node cannot tell a straight-flying unknown
  from a satellite; triangulation (orbital altitude, 80-120 km meteor
  heights) settles it on the server.

Satellite and meteor labels are never applied to a track that has
manoeuvred (sticky ``maneuvered`` flag), so a target that flies straight
and then turns is not hidden.

Filters are re-evaluated every frame. A verdict holds for ``hold_s`` after
the last frame that produced it (``sticky_s`` for aircraft), so a label does
not flicker on and off with measurement noise; ``slow`` has no hold, so an
object that hovers and then darts away is reported as soon as it moves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .adsb import Aircraft
from .config import FiltersConfig
from .flow import CloudFlow
from .geometry import angular_separation_array, angular_separation_deg, azel_to_enu_array
from .kinematics import mean_angular_speed
from .tracking import Track, TrackSample, Verdict


@dataclass
class FilterContext:
    t_ns: int
    observer: Optional[Tuple[float, float, float]] = None  # lat, lon, alt (HAE)
    aircraft: Sequence[Aircraft] = ()
    cloud_flow: Optional[CloudFlow] = None
    aircraft_now: List[Tuple[Aircraft, float, float]] = field(default_factory=list)  # (ac, az, el) at t_ns

    def prepare(self) -> None:
        """Compute every aircraft's az/el at the context time once per evaluation round."""
        self.aircraft_now = []
        if self.observer is None:
            return
        lat, lon, alt = self.observer
        for ac in self.aircraft:
            az, el, _ = ac.azel_from(lat, lon, alt, self.t_ns)
            if el > -2.0:
                self.aircraft_now.append((ac, az, el))


def _median(values: Sequence[float]) -> float:
    return float(np.median(np.asarray(values, dtype=np.float64)))


class FilterBank:
    def __init__(self, cfg: FiltersConfig, fps: float):
        self.cfg = cfg
        self.fps = fps

    def evaluate(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        """Update ``track.verdicts`` and return the active verdict, if any."""
        held = {k: v for k, v in track.verdicts.items() if v.sticky_until_ns > ctx.t_ns}
        verdict = None
        for check in (self.aircraft, self.cloud, self.bird, self.slow, self.satellite, self.meteor):
            verdict = check(track, ctx)
            if verdict is not None:
                break
        if verdict is not None:
            hold_ns = 0 if verdict.label == "slow" else int(self.cfg.hold_s * 1e9)
            verdict.sticky_until_ns = max(verdict.sticky_until_ns, ctx.t_ns + hold_ns)
            held.pop(verdict.label, None)
            held = {verdict.label: verdict, **held}
        track.verdicts = held
        return track.filtered_by()

    # --- individual filters ---------------------------------------------------

    def slow(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        c = self.cfg.slow
        if not c.enabled or track.age_s < c.min_age_s:
            return None
        speed = mean_angular_speed(track.recent(max(c.min_age_s, 3.0)))
        if speed is not None and speed < c.min_angular_speed_deg_s:
            return Verdict("slow", f"angular speed {speed:.4f} deg/s < {c.min_angular_speed_deg_s} "
                                   "(star, planet or stationary light)")
        return None

    def cloud(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        c = self.cfg.cloud
        recent = track.recent(1.0, ("det",))
        if not c.enabled or len(recent) < 3:
            return None
        eff = _median([s.eff_area for s in recent])
        snr = _median([s.snr for s in recent])
        if eff < c.min_effective_area_px or snr > c.max_peak_snr:
            return None
        speed = mean_angular_speed(recent)
        if speed is not None and speed <= c.max_angular_speed_deg_s:
            return Verdict("cloud", f"diffuse (effective area {eff:.0f} px, SNR {snr:.1f}) and slow "
                                    f"({speed:.2f} deg/s)")
        if ctx.cloud_flow is not None:
            flow = ctx.cloud_flow.velocity_at(track.last.u, track.last.v, max(16.0, math.sqrt(eff) * 2))
            if flow is not None:
                vu, vv = float(track.kf.x[2]), float(track.kf.x[3])
                fmag = math.hypot(*flow)
                mismatch = math.hypot(vu - flow[0], vv - flow[1])
                if fmag >= c.flow_match_px_s and mismatch <= max(c.flow_match_px_s, c.flow_match_fraction * fmag):
                    return Verdict("cloud", f"diffuse (effective area {eff:.0f} px, SNR {snr:.1f}) and moving "
                                            f"with the cloud field ({fmag:.1f} px/s, mismatch {mismatch:.1f})")
        return None

    def aircraft(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        c = self.cfg.aircraft
        if not c.enabled or not ctx.aircraft_now or ctx.observer is None:
            return None
        recent = track.recent(c.window_s)[-10:]
        if len(recent) < c.min_samples:
            return None
        lat, lon, alt = ctx.observer
        last = recent[-1]
        best: Optional[Tuple[float, Aircraft]] = None
        for ac, az, el in ctx.aircraft_now:
            gate = c.gate_deg if ac.alt_geometric else c.baro_gate_deg
            if angular_separation_deg(last.az, last.el, az, el) > 5.0 * gate + 1.0:
                continue
            pred = np.array([ac.azel_from(lat, lon, alt, s.t_ns)[:2] for s in recent])
            seps = angular_separation_array(np.array([s.az for s in recent]), np.array([s.el for s in recent]),
                                            pred[:, 0], pred[:, 1])
            med = float(np.median(seps))
            if med <= gate and (best is None or med < best[0]):
                best = (med, ac)
        if best is None:
            return None
        med, ac = best
        name = f"{ac.flight} " if ac.flight else ""
        return Verdict("aircraft", f"ADS-B {name}({ac.hex}) within {med:.2f} deg over {len(recent)} samples",
                       sticky_until_ns=ctx.t_ns + int(c.sticky_s * 1e9))

    def bird(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        c = self.cfg.bird
        recent = track.recent(c.window_s, ("det",))
        if not c.enabled or len(recent) < 8:
            return None
        area = np.array([max(s.area, 1) for s in recent], dtype=np.float64)
        med_area = float(np.median(area))
        if med_area < c.min_area_px:
            return None
        t = np.array([(s.t_ns - recent[0].t_ns) / 1e9 for s in recent])
        ln_a = np.log(area)
        slope = float(np.polyfit(t, ln_a, 1)[0])
        reasons = []
        expanding = abs(slope) >= c.expansion_rate_per_s
        if expanding:
            reasons.append(f"d ln(area)/dt {slope:+.2f}/s")
        # A quadratic detrend removes approach/retreat and slow fading, which
        # would otherwise leak into the lowest wingbeat frequencies.
        residual = ln_a - np.polyval(np.polyfit(t, ln_a, 2), t)
        beat = wingbeat(t, residual, self.fps, c.wingbeat_min_hz, c.wingbeat_max_hz)
        flapping = beat is not None and beat[1] >= c.wingbeat_min_power and beat[2] >= c.wingbeat_min_depth
        if flapping and beat is not None:
            reasons.append(f"wingbeat {beat[0]:.1f} Hz (power {beat[1]:.2f}, depth {beat[2]:.2f})")
        snr_per_px = _median([s.snr for s in recent]) / med_area
        defocused = snr_per_px <= c.defocus_max_snr_per_px
        if defocused:
            reasons.append(f"defocused (SNR/px {snr_per_px:.3f})")
        if not (flapping or (expanding and defocused)):
            return None
        return Verdict("bird", f"near-camera object, area {med_area:.0f} px: " + ", ".join(reasons))

    def satellite(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        c = self.cfg.satellite
        if not c.enabled or "maneuvered" in track.flags or track.age_s < c.min_age_s:
            return None
        samples = [s for s in track.samples if s.source == "det"]
        if len(samples) < 12:
            return None
        n = len(samples)
        speeds = [mean_angular_speed(samples[j * n // 4:(j + 1) * n // 4]) for j in range(4)]
        if any(s is None for s in speeds):
            return None
        sp = np.array(speeds, dtype=np.float64)
        mean_speed = float(sp.mean())
        if not (c.min_speed_deg_s <= mean_speed <= c.max_speed_deg_s) or sp.std() / mean_speed > c.max_speed_cv:
            return None
        xtrack = great_circle_deviation(samples)
        if xtrack > c.max_cross_track_deg:
            return None
        peaks = np.array([s.peak for s in samples])
        cv = float(peaks.std() / max(peaks.mean(), 1e-6))
        if cv > c.max_intensity_cv:
            return None
        return Verdict("satellite", f"steady {mean_speed:.2f} deg/s on a great circle (max deviation "
                                    f"{xtrack:.3f} deg), steady brightness (cv {cv:.2f})")

    def meteor(self, track: Track, ctx: FilterContext) -> Optional[Verdict]:
        c = self.cfg.meteor
        if not c.enabled or "maneuvered" in track.flags:
            return None
        samples = [s for s in track.samples if s.source == "det"]
        if len(samples) < 5 or (samples[-1].t_ns - track.first_ns) / 1e9 > c.max_duration_s:
            return None
        speed = mean_angular_speed(samples)
        if speed is None or speed < c.min_speed_deg_s:
            return None
        xtrack = great_circle_deviation(samples)
        if xtrack > c.max_cross_track_deg:
            return None
        return Verdict("meteor", f"fast straight streak {speed:.1f} deg/s, deviation {xtrack:.3f} deg")


def wingbeat(t: np.ndarray, x: np.ndarray, fps: float, f_min: float, f_max: float
             ) -> Optional[Tuple[float, float, float]]:
    """(frequency Hz, fraction of variance at the spectral peak, std of x) of a periodic series.

    ``x`` is resampled onto a uniform grid (tracks have gaps). The spectral
    peak must also agree with the zero-crossing rate of ``x`` (within a
    factor of 1.6) and show at least two full cycles; a residual trend that
    leaks into one low-frequency bin has far fewer crossings than a real
    oscillation of that frequency.
    """
    if len(t) < 8 or t[-1] - t[0] <= 0:
        return None
    span = float(t[-1] - t[0])
    n = int(round(span * fps)) + 1
    grid = np.linspace(t[0], t[-1], n)
    y = np.interp(grid, t, x)
    y = y - y.mean()
    power = np.abs(np.fft.rfft(y * np.hanning(n))) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / fps)
    total = float(power[1:].sum())
    band = (freqs >= max(f_min, 2.0 / span)) & (freqs <= min(f_max, fps / 2.0))
    if total <= 0 or not band.any():
        return None
    idx = np.nonzero(band)[0]
    k = int(idx[np.argmax(power[idx])])
    crossings = int(np.count_nonzero(np.signbit(y[1:]) != np.signbit(y[:-1])))
    f_cross = crossings / (2.0 * span)
    if crossings < 4 or not (freqs[k] / 1.6 <= f_cross <= freqs[k] * 1.6):
        return None
    peak_power = float(power[max(k - 1, 1):k + 2].sum())  # Hann window spreads a tone over ~3 bins
    return float(freqs[k]), peak_power / total, float(np.std(x))


def great_circle_deviation(samples: Sequence[TrackSample]) -> float:
    """Max angular distance (deg) of the samples from their best-fit great circle."""
    p = azel_to_enu_array(np.array([s.az for s in samples]), np.array([s.el for s in samples]))
    _, _, vt = np.linalg.svd(p, full_matrices=False)
    normal = vt[-1]
    return float(np.degrees(np.max(np.abs(np.arcsin(np.clip(p @ normal, -1.0, 1.0))))))
