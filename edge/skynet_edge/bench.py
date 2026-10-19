"""``skynet-edge bench``: the full node pipeline on synthetic 1080p60 video.

Measures what the blueprint's acceptance criterion asks for: sustained
processing of 1920x1080 at 60 fps within 7 GB of memory. The synthetic
source (``synthetic.py``, about 2 ms per frame) feeds the real capture
thread, ring buffer, detector, tracker, optical flow, filters, kinematics and
message signing (to a null uplink). By default frames arrive paced at the
camera rate, like a real sensor, and the queue drops frames if processing
falls behind; ``--unpaced`` instead applies backpressure to measure the
maximum throughput.

Numbers from a development machine say nothing about a Jetson: run the
bench on the target (see README "Validation on hardware") and watch
``tegrastats`` or ``jtop`` alongside it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

from .config import EdgeConfig, PositionConfig, parse_config
from .health import process_memory_mb, system_memory_mb
from .node import EdgeNode
from .uplink import NullUplink

FPS_TOLERANCE = 0.99  # sustained rate must reach 99% of the camera rate
MAX_DROP_FRACTION = 0.01
MEMORY_LIMIT_BYTES = 7e9  # "< 7 GB"


def bench_config(base: Optional[EdgeConfig], width: int, height: int, fps: float, paced: bool,
                 backend: Optional[str], clips_dir: Optional[str]) -> EdgeConfig:
    cfg = parse_config({}) if base is None else dataclasses.replace(base)
    cam = dataclasses.replace(cfg.camera, source="synthetic", width=width, height=height, fps=fps, realtime=paced,
                              intrinsics=None if (width, height) != (cfg.camera.width, cfg.camera.height)
                              else cfg.camera.intrinsics)
    det = cfg.detection if backend is None else dataclasses.replace(cfg.detection, backend=backend)
    gps = cfg.gps if cfg.gps.static is not None else dataclasses.replace(
        cfg.gps, source="static", static=PositionConfig(33.6292, -112.3679, 357.2))
    clips = dataclasses.replace(cfg.clips, record_events=clips_dir is not None,
                                storage_dir=clips_dir or tempfile.mkdtemp(prefix="skynet-bench-"))
    node = dataclasses.replace(cfg.node, key_hex=os.urandom(32).hex(), key_file=None)  # signing cost included
    return dataclasses.replace(
        cfg, node=node, camera=cam, detection=det, gps=gps, clips=clips,
        server=dataclasses.replace(cfg.server, transport="none"),
        attitude=dataclasses.replace(cfg.attitude, source="static"),
        adsb=dataclasses.replace(cfg.adsb, source="none"),
        health=dataclasses.replace(cfg.health, watchdog=False, chrony_offset=False))


def _percentiles(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    a = np.asarray(values)
    return {"mean": round(float(a.mean()), 3), "p50": round(float(np.percentile(a, 50)), 3),
            "p95": round(float(np.percentile(a, 95)), 3), "max": round(float(a.max()), 3)}


def run_bench(cfg: EdgeConfig, seconds: float, warmup_s: float = 2.0) -> Dict[str, Any]:
    node = EdgeNode(cfg, uplink=NullUplink(cfg.node.node_id, cfg.node.load_key()))
    timer = node.pipeline.timer
    marks: Dict[str, Any] = {}

    def controller() -> None:
        node.capture.opened.wait(60.0)
        time.sleep(warmup_s)
        for samples in timer.samples.values():
            samples.clear()
        timer.keep_samples = True
        marks.update(t0=time.perf_counter(), frames0=node.counters["frames"], captured0=node.capture.frames,
                     dropped0=node.queue.dropped)
        time.sleep(seconds)
        marks.update(t1=time.perf_counter(), frames1=node.counters["frames"], captured1=node.capture.frames,
                     dropped1=node.queue.dropped, telemetry=node.counters["telemetry"],
                     reported=node.pipeline.reported_tracks, filtered=dict(node.pipeline.labels))
        timer.keep_samples = False
        node.stop()

    ctl = threading.Thread(target=controller, name="bench", daemon=True)
    ctl.start()
    node.run(drain_timeout_s=5.0)
    ctl.join(5.0)
    if "t1" not in marks:
        raise RuntimeError("bench did not complete (capture failed?)")
    elapsed = marks["t1"] - marks["t0"]
    processed = marks["frames1"] - marks["frames0"]
    captured = marks["captured1"] - marks["captured0"]
    dropped = marks["dropped1"] - marks["dropped0"]
    mem = process_memory_mb()
    peak_bytes = mem["peak_rss_mb"] * 2**20
    fps = processed / elapsed if elapsed > 0 else 0.0
    target = cfg.camera.fps
    drop_fraction = dropped / max(1, captured)
    stages = {k: _percentiles(v) for k, v in timer.samples.items()}
    stages["ring_write"] = {"mean": round(node.capture.write_ms, 3)}
    checks = {
        "fps": fps >= FPS_TOLERANCE * target,
        "drops": drop_fraction <= MAX_DROP_FRACTION,
        "memory": peak_bytes < MEMORY_LIMIT_BYTES,
    }
    total = stages.get("total", {})
    return {
        "machine": {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(),
                    "python": platform.python_version(), "jetson": os.path.exists("/etc/nv_tegra_release")},
        "video": {"width": cfg.camera.width, "height": cfg.camera.height, "fps": target,
                  "paced": cfg.camera.realtime},
        "detector": node.pipeline.detector.name,
        "seconds": round(elapsed, 2),
        "frames_processed": processed,
        "frames_dropped": dropped,
        "sustained_fps": round(fps, 2),
        "max_fps_estimate": round(1000.0 / total["mean"], 1) if total.get("mean") else None,
        "stage_ms": stages,
        "ring_buffer_mib": round(node.ring.nbytes / 2**20, 1),
        "peak_rss_mib": mem["peak_rss_mb"],
        "system_memory_mib": system_memory_mb(),
        "telemetry_messages": marks["telemetry"],
        "tracks_reported": marks["reported"],
        "tracks_filtered": marks["filtered"],
        "checks": checks,
        "pass": all(checks.values()),
    }


def format_report(r: Dict[str, Any]) -> str:
    v, m = r["video"], r["machine"]
    where = "Jetson" if m["jetson"] else f"NOT a Jetson ({m['machine']}, {m['cpus']} CPUs)"
    lines = [
        f"SKY-NET edge bench on {where}: {m['platform']}",
        f"video {v['width']}x{v['height']} @ {v['fps']:g} fps ({'paced' if v['paced'] else 'unpaced'}), "
        f"detector {r['detector']}, {r['seconds']} s measured",
        f"sustained {r['sustained_fps']} fps, dropped {r['frames_dropped']} of "
        f"{r['frames_processed'] + r['frames_dropped']}, pipeline-only ceiling ~{r['max_fps_estimate']} fps",
        "stage ms      mean     p50     p95     max",
    ]
    for stage in ("detect", "track", "flow", "filter", "total"):
        s = r["stage_ms"].get(stage)
        if s:
            lines.append(f"  {stage:<9}{s['mean']:8.3f}{s['p50']:8.3f}{s['p95']:8.3f}{s['max']:8.3f}")
    lines.append(f"  ring_write {r['stage_ms']['ring_write']['mean']:7.3f} (capture thread)")
    lines.append(f"memory: ring buffer {r['ring_buffer_mib']} MiB, peak RSS {r['peak_rss_mib']} MiB")
    lines.append(f"tracks reported {r['tracks_reported']}, filtered {r['tracks_filtered']}, "
                 f"telemetry messages {r['telemetry_messages']}")
    c = r["checks"]
    lines.append(f"criterion >= {FPS_TOLERANCE * v['fps']:.1f} fps: {'PASS' if c['fps'] else 'FAIL'}; "
                 f"drops <= {MAX_DROP_FRACTION:.0%}: {'PASS' if c['drops'] else 'FAIL'}; "
                 f"memory < 7 GB: {'PASS' if c['memory'] else 'FAIL'}")
    lines.append("RESULT: " + ("PASS" if r["pass"] else "FAIL"))
    return "\n".join(lines)


def write_json(r: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2)
