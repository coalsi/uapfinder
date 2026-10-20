"""``skynet-edge`` command line.

Pipeline: run, bench, replay, make-synthetic. Appliance (docs/NODE_APPLIANCE.md):
detect, autoconfig, setup-server, netsetup, starcal, tailscale-join, admin-requests.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import shutil
import signal
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import __version__
from .config import ConfigError, EdgeConfig, load_config, parse_config

log = logging.getLogger("skynet_edge")


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load(path: Optional[str]) -> EdgeConfig:
    return load_config(path) if path else parse_config({})


def cmd_run(args: argparse.Namespace) -> int:
    import threading

    from .node import EdgeNode

    cfg = _load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    if cfg.node.credentials_file:
        from .appliance.credentials import load_credentials

        if load_credentials(cfg.node.credentials_file) is None:
            from .appliance.waiting import wait_for_enrollment

            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            wait_for_enrollment(cfg, stop)
            return 0  # systemd restarts the service, which re-runs autoconfig with the credentials
    node = EdgeNode(cfg)

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %d: stopping", signum)
        node.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    return node.run()


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import bench_config, format_report, run_bench, write_json

    _setup_logging(args.log_level or "WARNING")
    base = load_config(args.config) if args.config else None
    cfg = bench_config(base, args.width, args.height, args.fps, not args.unpaced, args.backend, args.clips_dir)
    report = run_bench(cfg, args.seconds)
    print(format_report(report))
    if args.json:
        write_json(report, args.json)
    return 0 if report["pass"] else 2


def _probe_video(path: str) -> Dict[str, float]:
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ConfigError(f"cannot open video {path}")
    info = {"width": cap.get(cv2.CAP_PROP_FRAME_WIDTH), "height": cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "fps": cap.get(cv2.CAP_PROP_FPS)}
    cap.release()
    return info


def cmd_replay(args: argparse.Namespace) -> int:
    from .node import EdgeNode
    from .uplink import NullUplink

    cfg = _load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    info = _probe_video(args.video)
    width, height = int(info["width"]), int(info["height"])
    if cfg.camera.intrinsics is not None and (width, height) != (cfg.camera.width, cfg.camera.height):
        raise ConfigError(f"video is {width}x{height} but the calibrated intrinsics are for "
                          f"{cfg.camera.width}x{cfg.camera.height}")
    if args.fps:
        fps = args.fps
    elif cfg.camera.source == "file":  # a replay config (e.g. from make-synthetic) knows the true rate
        fps = cfg.camera.fps
    else:
        fps = info["fps"] or cfg.camera.fps
    start_ns = args.start_ns if args.start_ns is not None else cfg.camera.file_start_ns
    cam = dataclasses.replace(cfg.camera, source="file", file_path=args.video, file_loop=False, width=width,
                              height=height, fps=float(fps), realtime=args.realtime, file_start_ns=start_ns)
    clips = cfg.clips if args.clips_dir is None else dataclasses.replace(cfg.clips, storage_dir=args.clips_dir)
    if args.clips_dir is None:
        clips = dataclasses.replace(clips, record_events=False)
    cfg = dataclasses.replace(cfg, camera=cam, clips=clips,
                              health=dataclasses.replace(cfg.health, watchdog=False, chrony_offset=False))
    out = open(args.out, "wb") if args.out else None
    tracks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    def sink(data: bytes) -> None:
        msg = json.loads(data)
        if msg.get("type") == "telemetry":
            tracks[msg["track_id"]].append(msg)
        if out is not None:
            out.write(data + b"\n")

    node = EdgeNode(cfg, uplink=None if args.send else NullUplink(cfg.node.node_id, cfg.node.load_key(), sink))
    signal.signal(signal.SIGINT, lambda *_: node.stop())
    code = node.run()
    if out is not None:
        out.close()
    print(f"replayed {node.counters['frames']} frames of {args.video}: "
          f"{len(tracks) if not args.send else node.pipeline.reported_tracks} reported track(s), "
          f"filtered {dict(node.pipeline.labels)}")
    for tid, msgs in tracks.items():
        t0, t1 = msgs[0]["timestamp_ns"], msgs[-1]["timestamp_ns"]
        print(f"  {tid}: {len(msgs)} reports over {(t1 - t0) / 1e9:.2f} s, max anomaly "
              f"{max(m['anomaly_score'] for m in msgs):.2f}, first az/el "
              f"{msgs[0]['vector_target']['azimuth_deg']:.3f}/{msgs[0]['vector_target']['elevation_deg']:.3f}")
    return code


def cmd_make_synthetic(args: argparse.Namespace) -> int:
    from .synthetic import DEFAULT_T0_NS, standard_scene

    _setup_logging(args.log_level or "INFO")
    base = _load(args.config)
    cam = dataclasses.replace(base.camera, source="synthetic", width=args.width, height=args.height, fps=args.fps,
                              intrinsics=None if args.config is None else base.camera.intrinsics)
    cfg = dataclasses.replace(base, camera=cam)
    t0 = args.t0_ns if args.t0_ns is not None else DEFAULT_T0_NS
    scene = standard_scene(cfg, t0_ns=t0, seed=args.seed, loop=False)
    n = int(round(args.seconds * args.fps))
    out = Path(args.output)
    raw = out.suffix.lower() == ".y4m"
    if raw:
        from .clips import Y4MWriter

        writer = Y4MWriter(out, args.width, args.height, args.fps)
        for i in range(n):
            writer.write(scene.render(i, t0 + int(round(i * 1e9 / args.fps))))
        writer.close()
    else:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            print("ffmpeg not found; write a .y4m file instead", file=sys.stderr)
            return 1
        proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                 "-pix_fmt", "yuv420p", "-s", f"{args.width}x{args.height}", "-r", f"{args.fps:g}",
                                 "-i", "-", "-c:v", "libx264", "-preset", "fast", "-crf", str(args.crf),
                                 "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
        assert proc.stdin is not None
        for i in range(n):
            proc.stdin.write(scene.render(i, t0 + int(round(i * 1e9 / args.fps))).tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            return 1
    stem = out.with_suffix("")
    adsb_path = Path(f"{stem}.aircraft.json")
    adsb_path.write_text(json.dumps(scene.adsb_json(t0)), encoding="utf-8")
    truth = {"t0_ns": t0, "fps": args.fps, "frames": n, "objects": {}}
    for obj in scene.objects:
        rows = []
        for i in range(n):
            t_ns = t0 + int(round(i * 1e9 / args.fps))
            azel = scene.truth(obj.name, t_ns)
            if azel is not None:
                rows.append([t_ns, round(azel[0], 6), round(azel[1], 6)])
        truth["objects"][obj.name] = {"kind": obj.kind, "samples": rows}
    Path(f"{stem}.truth.json").write_text(json.dumps(truth), encoding="utf-8")
    replay_cfg = {
        "node": {"node_id": cfg.node.node_id},
        "server": {"transport": "none"},
        "camera": {"source": "file", "file_path": str(out), "file_start_ns": t0, "width": args.width,
                   "height": args.height, "fps": args.fps, "fov_h_deg": cam.fov_h_deg,
                   **({"fov_v_deg": cam.fov_v_deg} if cam.fov_v_deg is not None else {})},
        "attitude": {"source": "static", "yaw_deg": cfg.attitude.yaw_deg, "pitch_deg": cfg.attitude.pitch_deg,
                     "roll_deg": cfg.attitude.roll_deg},
        "gps": {"source": "static", "static": {"latitude": scene.observer[0], "longitude": scene.observer[1],
                                               "altitude_m": scene.observer[2]}},
        "adsb": {"source": "dump1090", "file_path": str(adsb_path), "max_position_age_s": args.seconds + 15},
        "clips": {"record_events": False, "storage_dir": str(Path(f"{stem}.clips"))},
    }
    Path(f"{stem}.yaml").write_text(yaml.safe_dump(replay_cfg, sort_keys=False), encoding="utf-8")
    print(f"wrote {out} ({n} frames), {stem}.truth.json, {adsb_path.name}, {stem}.yaml\n"
          f"replay with: skynet-edge replay {out} --config {stem}.yaml")
    return 0


def _exit_hard(code: int) -> int:
    """Exit even if a probe thread is stuck in a driver call (concurrent.futures joins its workers at exit)."""
    sys.stdout.flush()
    sys.stderr.flush()
    import threading

    if any(t.name.startswith(("detect", "gps-probe", "i2c-probe")) and t.is_alive() for t in threading.enumerate()):
        import os

        os._exit(code)
    return code


def cmd_detect(args: argparse.Namespace) -> int:
    from .appliance.detect import Detector

    _setup_logging(args.log_level or "WARNING")
    result = Detector(deadline_s=args.deadline).detect()
    print(json.dumps(result, indent=2, sort_keys=True))
    return _exit_hard(0)


def cmd_autoconfig(args: argparse.Namespace) -> int:
    import os

    from .appliance.autoconfig import autoconfig, update_chrony
    from .appliance.paths import Paths

    _setup_logging(args.log_level or "INFO")
    paths = Paths.from_env()
    result = autoconfig(paths, write=not args.dry_run)
    if args.dry_run:
        print(yaml.safe_dump(result["config"], sort_keys=False), end="")
    else:
        log.info("autoconfig: %s -> %s%s", json.dumps(result["summary"], sort_keys=True), paths.edge_yaml,
                 "" if result["ok"] else f" ({result['error']})")
    if args.system and not args.dry_run:
        if os.geteuid() != 0:
            log.warning("--system needs root; chrony configuration left alone")
        else:
            update_chrony(result["detect"], paths)
    return _exit_hard(0)


def cmd_setup_server(args: argparse.Namespace) -> int:
    from .appliance.web.server import serve

    _setup_logging(args.log_level or "INFO")
    return serve(args.bind, args.port)


def cmd_netsetup(args: argparse.Namespace) -> int:
    from .appliance.netsetup import main_loop

    _setup_logging(args.log_level or "INFO")
    return main_loop()


def cmd_starcal(args: argparse.Namespace) -> int:
    from .appliance.starcal_job import run_job

    _setup_logging(args.log_level or "INFO")
    summary = run_job(force=args.force)
    print(json.dumps(summary, sort_keys=True))
    return 0


def cmd_tailscale_join(args: argparse.Namespace) -> int:
    from .appliance.tailscale import join

    _setup_logging(args.log_level or "INFO")
    return join(wait_s=args.wait)


def cmd_admin_requests(args: argparse.Namespace) -> int:
    from .appliance.admin import process_spool

    _setup_logging(args.log_level or "INFO")
    for _name, ok, _msg in process_spool():
        if not ok:
            return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skynet-edge", description="SKY-NET edge node")
    p.add_argument("--version", action="version", version=f"skynet-edge {__version__}")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the node (camera, detection, uplink)")
    r.add_argument("--config", required=True, help="YAML configuration file")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("bench", help="benchmark the pipeline on synthetic video")
    b.add_argument("--config", help="use this config's detection/tracking/filter settings")
    b.add_argument("--seconds", type=float, default=20.0)
    b.add_argument("--width", type=int, default=1920)
    b.add_argument("--height", type=int, default=1080)
    b.add_argument("--fps", type=float, default=60.0)
    b.add_argument("--backend", choices=["auto", "cpu", "cuda"], default=None)
    b.add_argument("--unpaced", action="store_true", help="as fast as possible instead of at the camera rate")
    b.add_argument("--clips-dir", help="also record event clips into this directory (exercises disk I/O)")
    b.add_argument("--json", help="write the full report as JSON")
    b.set_defaults(func=cmd_bench)

    rp = sub.add_parser("replay", help="run the pipeline on a video file and print what would be reported")
    rp.add_argument("video")
    rp.add_argument("--config", help="YAML configuration (camera geometry, attitude, position, filters)")
    rp.add_argument("--fps", type=float, help="frame rate for timestamps (default: the video's)")
    rp.add_argument("--start-ns", type=int, help="capture time of the first frame (Unix ns)")
    rp.add_argument("--realtime", action="store_true", help="pace frames at the video rate")
    rp.add_argument("--send", action="store_true", help="send telemetry to the configured server")
    rp.add_argument("--out", help="write the signed telemetry/heartbeat messages as NDJSON")
    rp.add_argument("--clips-dir", help="record event clips into this directory")
    rp.set_defaults(func=cmd_replay)

    m = sub.add_parser("make-synthetic", help="render the synthetic test sky to a video with ground truth")
    m.add_argument("output", help=".mp4 (libx264 via ffmpeg) or .y4m (raw)")
    m.add_argument("--config", help="camera/attitude/position to render for")
    m.add_argument("--seconds", type=float, default=8.0)
    m.add_argument("--width", type=int, default=1280)
    m.add_argument("--height", type=int, default=720)
    m.add_argument("--fps", type=float, default=30.0)
    m.add_argument("--crf", type=int, default=12)
    m.add_argument("--seed", type=int, default=1)
    m.add_argument("--t0-ns", type=int, help="timestamp of the first frame (default 2026-09-21T13:33:20Z)")
    m.set_defaults(func=cmd_make_synthetic)

    d = sub.add_parser("detect", help="print the attached hardware as JSON")
    d.add_argument("--deadline", type=float, default=8.0, help="overall time limit in seconds")
    d.set_defaults(func=cmd_detect)

    ac = sub.add_parser("autoconfig", help="write /etc/skynet/edge.yaml from hardware, credentials and calibration")
    ac.add_argument("--dry-run", action="store_true", help="print the configuration instead of writing it")
    ac.add_argument("--system", action="store_true", help="as root: also update chrony's GPS/PPS refclocks")
    ac.set_defaults(func=cmd_autoconfig)

    ss = sub.add_parser("setup-server", help="serve the setup and status page")
    ss.add_argument("--bind", default="0.0.0.0")
    ss.add_argument("--port", type=int, default=80)
    ss.set_defaults(func=cmd_setup_server)

    ns = sub.add_parser("netsetup", help="(root) fallback Wi-Fi hotspot and the uapfinder.local alias")
    ns.set_defaults(func=cmd_netsetup)

    sc = sub.add_parser("starcal", help="star calibration, if it is dark, there is a fix and it is due")
    sc.add_argument("--force", action="store_true", help="recalibrate even if the calibration is recent")
    sc.set_defaults(func=cmd_starcal)

    tj = sub.add_parser("tailscale-join", help="(root) join the tailnet with the enrollment's auth key")
    tj.add_argument("--wait", type=float, default=90.0, help="seconds to wait for the tailnet")
    tj.set_defaults(func=cmd_tailscale_join)

    ar = sub.add_parser("admin-requests", help="(root) carry out queued setup-page requests")
    ar.set_defaults(func=cmd_admin_requests)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
