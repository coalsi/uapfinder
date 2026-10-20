"""The node's appliance hooks: node.json, preview/stack tap, restart on new credentials, waiting mode."""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest
import yaml

from helpers import T0_NS, small_config
from skynet_edge.appliance.credentials import save_credentials, validate_enrollment
from skynet_edge.appliance.waiting import wait_for_enrollment
from skynet_edge.capture import FrameSource
from skynet_edge.cli import main
from skynet_edge.node import EdgeNode

KEY = "ef" * 32
CREDS = {"node_id": "node-w", "node_key": KEY, "transport": "udp", "udp_host": "127.0.0.1", "udp_port": 9399}


class FakeCam(FrameSource):
    live = True

    def __init__(self, w=64, h=48):
        super().__init__(w, h, 30.0)
        self.n = 0
        self.opened = 0

    def open(self):
        self.opened += 1

    def read(self):
        self.n += 1
        time.sleep(0.01)
        return np.full((self.height * 3 // 2, self.width), self.n % 255, np.uint8), T0_NS + self.n * 33_333_333


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_running_node_publishes_status_preview_and_restarts_on_new_credentials(tmp_path):
    pytest.importorskip("cv2")
    run, state = tmp_path / "run", tmp_path / "state"
    run.mkdir()
    state.mkdir()
    creds = state / "credentials.json"
    cfg = small_config({"clips": {"storage_dir": str(tmp_path / "clips")},
                        "attitude": {"pointing_source": "prior", "pointing_sigma_deg": 45.0},
                        "appliance": {"run_dir": str(run), "restart_on_change": [str(creds)],
                                      "status_interval_s": 0.2, "preview_interval_s": 0.2}}, width=64, height=48)
    (run / "preview.request").touch()
    (run / "stack.request").write_text(json.dumps({"id": "s1", "frames": 3, "every": 2}))
    node = EdgeNode(cfg, source=FakeCam())
    node.watcher.interval_s = 0.1
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", node.run(drain_timeout_s=1.0)), daemon=True)
    t.start()
    try:
        assert wait_for(lambda: (run / "node.json").exists() and (run / "preview.jpg").exists()
                        and (run / "stack.json").exists())
        st = json.loads((run / "node.json").read_text())
        assert st["mode"] == "running" and st["node_id"] == "node-test" and st["pointing_source"] == "prior"
        assert st["camera"]["width"] == 64 and st["fix"]["latitude"] == pytest.approx(33.6292)
        assert st["status"]["pointing"] == "prior" and st["status"]["pointing_sigma_deg"] == 45.0
        meta = json.loads((run / "stack.json").read_text())
        assert meta["id"] == "s1" and meta["n"] == 3 and meta["width"] == 64
        assert np.load(run / "stack.npy").shape == (48, 64)
        hb = node.heartbeat_message()
        assert hb["status"]["pointing"] == "prior"
        save_credentials(creds, validate_enrollment(CREDS))
        t.join(15)
        assert not t.is_alive() and result["code"] == 0 and node.restart_requested
    finally:
        node.stop()
        t.join(5)


def test_waiting_mode_serves_preview_and_ends_on_enrollment(tmp_path):
    pytest.importorskip("cv2")
    run = tmp_path / "run"
    run.mkdir()
    creds = tmp_path / "credentials.json"
    cfg = small_config({"node": {"credentials_file": str(creds)},
                        "appliance": {"run_dir": str(run), "status_interval_s": 0.1, "preview_interval_s": 0.2}},
                       width=64, height=48)
    cam = FakeCam()
    stop = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", wait_for_enrollment(cfg, stop, poll_s=0.05,
                                                                                camera_factory=lambda: cam)),
                         daemon=True)
    t.start()
    try:
        assert wait_for(lambda: (run / "node.json").exists())
        assert json.loads((run / "node.json").read_text())["mode"] == "waiting"
        time.sleep(0.3)
        assert cam.opened == 0 and not (run / "preview.jpg").exists()  # camera only on demand
        (run / "preview.request").touch()
        assert wait_for(lambda: (run / "preview.jpg").exists())
        assert cam.opened == 1
        save_credentials(creds, validate_enrollment(CREDS))
        t.join(5)
        assert out["r"] is True
    finally:
        stop.set()
        t.join(5)


def test_waiting_mode_stops_on_signal(tmp_path):
    cfg = small_config({"node": {"credentials_file": str(tmp_path / "none.json")}})
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    assert wait_for_enrollment(cfg, stop, poll_s=0.05) is False


def test_cli_run_waits_until_enrolled(tmp_path):
    creds = tmp_path / "credentials.json"
    conf = tmp_path / "edge.yaml"
    conf.write_text(yaml.safe_dump({"node": {"node_id": "unenrolled", "credentials_file": str(creds)},
                                    "server": {"transport": "none"}, "camera": {"source": "synthetic"},
                                    "gps": {"source": "static", "static": {"latitude": 1.0, "longitude": 2.0,
                                                                          "altitude_m": 3.0}},
                                    "health": {"watchdog": False, "chrony_offset": False}}))
    timer = threading.Timer(0.6, lambda: save_credentials(creds, validate_enrollment(CREDS)))
    timer.start()
    t0 = time.monotonic()
    assert main(["run", "--config", str(conf)]) == 0  # waited (no failure) until the credentials appeared
    assert time.monotonic() - t0 >= 0.5
    timer.join()


def test_key_from_credentials_file(tmp_path):
    creds = tmp_path / "c.json"
    save_credentials(creds, validate_enrollment(CREDS))
    cfg = small_config({"node": {"credentials_file": str(creds)}})
    assert cfg.node.load_key() == bytes.fromhex(KEY)
