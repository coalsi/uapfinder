import json
import shutil

import pytest

from skynet_common.protocol import decode_message
from skynet_edge.cli import main
from skynet_edge.config import load_config

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_make_synthetic_then_replay(tmp_path, capsys):
    scene_cfg = tmp_path / "scene.yaml"
    scene_cfg.write_text("camera: {fov_h_deg: 60}\nattitude: {yaw_deg: 200, pitch_deg: 40, roll_deg: 2}\n"
                         "ring_buffer: {seconds: 6, hold_seconds: 1, prefault: false}\n")
    video = tmp_path / "sky.mp4"
    assert main(["make-synthetic", str(video), "--config", str(scene_cfg), "--width", "640", "--height", "360",
                 "--fps", "30", "--seconds", "7"]) == 0
    replay_cfg = tmp_path / "sky.yaml"
    truth = json.loads((tmp_path / "sky.truth.json").read_text())
    assert set(truth["objects"]) == {"target", "cloud", "bird", "aircraft"}
    cfg = load_config(replay_cfg)
    assert cfg.camera.file_start_ns == truth["t0_ns"] and cfg.adsb.source == "dump1090"
    out = tmp_path / "telemetry.ndjson"
    capsys.readouterr()
    assert main(["--log-level", "WARNING", "replay", str(video), "--config", str(replay_cfg),
                 "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 reported track(s)" in printed
    msgs = [decode_message(line, None, require_signature=False) for line in out.read_bytes().splitlines()]
    tel = [m for m in msgs if m.type == "telemetry"]
    assert len(tel) > 40 and len({m.track_id for m in tel}) == 1


def test_bench_smoke(tmp_path, capsys):
    report_path = tmp_path / "bench.json"
    code = main(["bench", "--seconds", "1", "--width", "320", "--height", "180", "--fps", "30",
                 "--json", str(report_path)])
    text = capsys.readouterr().out
    report = json.loads(report_path.read_text())
    assert code in (0, 2) and ("RESULT: PASS" in text) == (code == 0)
    assert report["video"] == {"width": 320, "height": 180, "fps": 30.0, "paced": True}
    assert set(report["checks"]) == {"fps", "drops", "memory"}
    assert report["frames_processed"] > 20 and "detect" in report["stage_ms"]
    assert text.startswith("SKY-NET edge bench on ") and "stage ms" in text


def test_config_errors_exit_cleanly(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("camera: {fps: fast}\n")
    assert main(["run", "--config", str(bad)]) == 2
    assert "configuration error" in capsys.readouterr().err
