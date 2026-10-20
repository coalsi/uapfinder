"""Credentials, enrollment client, autoconfig and the Tailscale join."""

from __future__ import annotations

import json
import os
import stat

import pytest
import yaml

from skynet_edge.appliance import autoconfig as ac
from skynet_edge.appliance import tailscale
from skynet_edge.appliance.calibration import save_calibration
from skynet_edge.appliance.credentials import (CredentialsError, EnrollError, enroll, format_code, hardware_summary,
                                               load_credentials, normalize_code, save_credentials,
                                               strip_tailscale_key, validate_enrollment)
from skynet_edge.appliance.paths import Paths
from skynet_edge.appliance.util import CmdResult, redact
from skynet_edge.config import load_config, parse_config

KEY = "ab" * 32
RESP = {"node_id": "4f9a2c1e-7b3d-4e8a-9c6f-1a2b3c4d5e6f", "node_key": KEY, "transport": "udp",
        "udp_host": "mac-studio", "udp_port": 9999, "https_base_url": "http://mac-studio:8000",
        "tailscale": {"auth_key": "tskey-auth-kAbC123-XyZ789qwerty", "tags": ["tag:skynet-node"]}}


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path / "etc", tmp_path / "state", tmp_path / "run")
    for d in (p.etc, p.state, p.run):
        d.mkdir()
    return p


# --- claim codes and responses -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["7KQ2-M9XD", "7kq2m9xd", " 7kq2 - m9xd ", "7KQ2M9XD"])
def test_code_normalisation(raw):
    assert normalize_code(raw) == "7KQ2M9XD" and format_code(raw) == "7KQ2-M9XD"


def test_code_crockford_aliases_and_rejects():
    assert normalize_code("oil0-1lio") == "0110" + "1110"
    for bad in ("7KQ2-M9X", "7KQ2-M9XDD", "7KQ2-M9XU", "7KQ2_M9XD", 12345678, None, "7KQ2;M9XD"):
        with pytest.raises(CredentialsError):
            normalize_code(bad)


def test_validate_enrollment_accepts_the_contract_example():
    out = validate_enrollment(RESP)
    assert out["node_key"] == KEY and out["tailscale"]["tags"] == ["tag:skynet-node"]
    assert validate_enrollment({**RESP, "tailscale": None})["tailscale"] is None


@pytest.mark.parametrize("change", [
    {"node_id": "a b"}, {"node_key": "zz" * 32}, {"node_key": "ab" * 31}, {"transport": "tcp"},
    {"udp_host": "mac-studio; rm -rf /"}, {"udp_host": "-oProxyCommand"}, {"udp_port": 0}, {"udp_port": "9999"},
    {"https_base_url": "javascript:alert(1)"}, {"https_base_url": "http://x/ y"},
    {"tailscale": {"auth_key": "--accept-routes", "tags": []}}, {"tailscale": {"tags": ["tag:a,tag:b"]}},
    {"tailscale": {"tags": ["--advertise-exit-node"]}}, {"tailscale": "yes"},
])
def test_validate_enrollment_rejects(change):
    with pytest.raises(CredentialsError):
        validate_enrollment({**RESP, **change})


def test_credentials_file_mode_and_public_view(paths):
    save_credentials(paths.credentials, validate_enrollment(RESP))
    assert stat.S_IMODE(os.stat(paths.credentials).st_mode) == 0o600
    creds = load_credentials(paths.credentials)
    pub = creds.public()
    assert "node_key" not in pub and "auth_key" not in pub["tailscale"] and pub["tailscale"]["auth_key_present"]
    assert KEY not in json.dumps(pub) and "tskey" not in json.dumps(pub)
    assert strip_tailscale_key(paths.credentials) is True
    assert stat.S_IMODE(os.stat(paths.credentials).st_mode) == 0o600
    again = load_credentials(paths.credentials)
    assert again.tailscale == {"tags": ["tag:skynet-node"], "joined": True}
    assert strip_tailscale_key(paths.credentials) is False


def test_corrupt_credentials_are_ignored(paths):
    paths.credentials.write_text("{not json")
    assert load_credentials(paths.credentials) is None
    paths.credentials.write_text(json.dumps({**RESP, "node_key": "short"}))
    assert load_credentials(paths.credentials) is None


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return self.resp


def test_enroll_client_success_and_errors():
    s = FakeSession(FakeResp(201, RESP))
    out = enroll("https://enroll.example.org/", "7kq2-m9xd", {"model": "x"}, session=s)
    assert s.calls == [("https://enroll.example.org/api/v1/enroll", {"code": "7KQ2M9XD", "hardware": {"model": "x"}})]
    assert out["node_id"] == RESP["node_id"] and out["enroll_url"] == "https://enroll.example.org"
    for status, needle in ((404, "not known"), (410, "already been used"), (429, "Too many"), (500, "500")):
        with pytest.raises(EnrollError, match=needle) as ei:
            enroll("https://e.example.org", "7KQ2M9XD", {}, session=FakeSession(FakeResp(status, {})))
        assert ei.value.status == status
    with pytest.raises(EnrollError, match="invalid response"):
        enroll("https://e.example.org", "7KQ2M9XD", {}, session=FakeSession(FakeResp(201, {"node_id": "x"})))
    with pytest.raises(CredentialsError):
        enroll("file:///etc/passwd", "7KQ2M9XD", {}, session=s)


def test_hardware_summary():
    det = {"platform": {"model": "NVIDIA Jetson Orin Nano Developer Kit", "serial": "1421224012345"},
           "camera": {"kind": "csi", "sensor": "imx477"}, "gps": {"kind": "gpsd"}, "pps": {"device": "/dev/pps1"},
           "imu": {"kind": "bno055"}, "adsb": {"rtlsdr": True}, "nvme": [{}]}
    hw = hardware_summary(det, "0.2.0")
    assert hw == {"model": "NVIDIA Jetson Orin Nano Developer Kit", "serial": "1421224012345", "camera": "imx477",
                  "gps": "pps", "software_version": "0.2.0", "extras": ["bno055", "rtlsdr", "nvme"]}
    assert hardware_summary(None, "0.2.0")["gps"] == "none"


def test_redaction():
    text = f"key {KEY} ts tskey-auth-abc123-DEF password=hunter22 \"node_key\": \"{KEY}\""
    out = redact(text)
    assert KEY not in out and "tskey-auth-abc" not in out and "hunter22" not in out


# --- autoconfig ---------------------------------------------------------------------------

NOTHING = {"camera": {"kind": "none"}, "gps": {"kind": "none"}, "pps": {"device": None}, "imu": {"kind": "none"},
           "adsb": {"rtlsdr": False}, "nvme": []}
FULL = {"camera": {"kind": "csi", "sensor": "imx477", "sensor_id": 0}, "gps": {"kind": "gpsd", "gpsd": True},
        "pps": {"device": "/dev/pps1"}, "imu": {"kind": "bno055", "bus": 7, "address": 0x29},
        "adsb": {"rtlsdr": True}, "nvme": []}
CALIB = {"version": 1, "ok": True, "source": "starcal", "t_ns": 1_790_000_000_000_000_000, "width": 1920,
         "height": 1080, "yaw_deg": 212.5, "pitch_deg": 71.25, "roll_deg": -1.5, "fx": 1010.0, "fy": 1012.0,
         "cx": 961.0, "cy": 541.0, "k1": -0.21, "k2": 0.04, "stars_matched": 57, "rms_deg": 0.031}


def test_nothing_attached_gives_a_valid_waiting_config(paths):
    res = ac.autoconfig(paths, det=NOTHING)
    assert res["ok"]
    cfg = load_config(paths.edge_yaml)
    assert cfg.node.node_id == "unenrolled" and cfg.node.credentials_file == str(paths.credentials)
    assert cfg.server.transport == "none"
    assert cfg.camera.source == "gstreamer" and cfg.camera.fov_h_deg == 90.0 and cfg.camera.intrinsics is None
    assert cfg.attitude.pointing_source == "prior" and cfg.attitude.pointing_sigma_deg == ac.SIGMA_PRIOR_DEG
    assert cfg.attitude.pitch_deg == 90.0
    assert cfg.adsb.source == "adsbx_v2" and "{lat}" in cfg.adsb.url and cfg.adsb.query_decimals == 2
    assert cfg.adsb.poll_interval_s >= 5.0
    assert cfg.appliance.run_dir == str(paths.run)
    assert paths.edge_yaml.read_text().startswith("# Generated by `skynet-edge autoconfig`")
    assert json.loads(paths.detect_json.read_text())["camera"] == {"kind": "none"}


def test_full_kit_with_credentials_and_calibration(paths):
    save_credentials(paths.credentials, validate_enrollment(RESP))
    save_calibration(paths.calibration, CALIB)
    ac.autoconfig(paths, det=FULL)
    cfg = load_config(paths.edge_yaml)
    assert cfg.node.node_id == RESP["node_id"] and cfg.node.load_key() == bytes.fromhex(KEY)
    assert (cfg.server.transport, cfg.server.udp_host, cfg.server.udp_port) == ("udp", "mac-studio", 9999)
    assert cfg.server.https_base_url == "http://mac-studio:8000"
    i = cfg.camera.intrinsics
    assert (i.fx, i.fy, i.cx, i.cy, i.dist) == (1010.0, 1012.0, 961.0, 541.0, [-0.21, 0.04, 0.0, 0.0])
    a = cfg.attitude
    assert (a.source, a.yaw_deg, a.pitch_deg, a.roll_deg) == ("static", 212.5, 71.25, -1.5)
    assert a.pointing_source == "starcal" and a.pointing_sigma_deg == pytest.approx(0.031)
    assert cfg.adsb.source == "dump1090" and cfg.adsb.file_path == "/run/readsb/aircraft.json"
    assert "tskey" not in paths.edge_yaml.read_text() and KEY not in paths.edge_yaml.read_text()


def test_imu_without_calibration_and_uvc_camera(paths):
    det = dict(FULL, camera={"kind": "uvc", "device": "/dev/video2"}, adsb={"rtlsdr": False})
    cfg = parse_config(ac.autoconfig(paths, det=det, write=False)["config"])
    assert cfg.camera.source == "v4l2" and cfg.camera.device == "/dev/video2"
    assert cfg.attitude.source == "bno055" and cfg.attitude.bno055.i2c_bus == 7 and cfg.attitude.bno055.address == 0x29
    assert cfg.attitude.pointing_source == "imu"


def test_calibration_for_another_resolution_keeps_pointing_only(paths):
    save_calibration(paths.calibration, dict(CALIB, width=4032, height=3040))
    cfg = parse_config(ac.autoconfig(paths, det=FULL, write=False)["config"])
    assert cfg.camera.intrinsics is None and cfg.attitude.yaw_deg == 212.5


def test_serial_gps_without_gpsd(paths):
    det = dict(NOTHING, gps={"kind": "serial", "device": "/dev/ttyACM0", "baud": 38400, "gpsd": False})
    cfg = parse_config(ac.autoconfig(paths, det=det, write=False)["config"])
    assert (cfg.gps.source, cfg.gps.nmea_device, cfg.gps.nmea_baud) == ("nmea", "/dev/ttyACM0", 38400)


def test_local_overrides_merge_and_bad_ones_are_ignored(paths):
    paths.local_yaml.write_text(yaml.safe_dump({"camera": {"fov_h_deg": 120.0}, "adsb_online": False,
                                                "gps": {"source": "static", "static": {"latitude": 1.0,
                                                        "longitude": 2.0, "altitude_m": 3.0}}}))
    res = ac.autoconfig(paths, det=NOTHING)
    cfg = load_config(paths.edge_yaml)
    assert res["ok"] and cfg.camera.fov_h_deg == 120.0 and cfg.camera.width == 1920
    assert cfg.adsb.source == "none" and cfg.gps.static.latitude == 1.0
    paths.local_yaml.write_text(yaml.safe_dump({"camera": {"fov_h_deg": 500}}))
    res = ac.autoconfig(paths, det=NOTHING)
    assert not res["ok"] and "edge.local.yaml ignored" in res["error"]
    assert load_config(paths.edge_yaml).camera.fov_h_deg == 90.0
    assert json.loads((paths.run / "autoconfig.json").read_text())["ok"] is False
    paths.local_yaml.write_text("camera: [unclosed")
    assert "invalid YAML" in ac.autoconfig(paths, det=NOTHING)["error"]


def test_hand_configured_key_wins(paths):
    keyfile = paths.etc / "node.key"
    keyfile.write_text(KEY)
    paths.local_yaml.write_text(yaml.safe_dump({"node": {"node_id": "node-a", "key_file": str(keyfile)},
                                                "server": {"transport": "udp", "udp_host": "10.0.0.5"}}))
    cfg = parse_config(ac.autoconfig(paths, det=NOTHING, write=False)["config"])
    assert cfg.node.credentials_file is None and cfg.node.node_id == "node-a" and cfg.server.udp_host == "10.0.0.5"


def test_defaults_file_and_setup_override(paths):
    paths.defaults_yaml.write_text(yaml.safe_dump({"enroll_url": "https://a.example.org", "lens_fov_h_deg": 100,
                                                   "adsb_online_url": "https://api.airplanes.live/v2/point/{lat}/{lon}/{dist}",
                                                   "unknown_key": 1}))
    d = ac.load_defaults(paths)
    assert d["enroll_url"] == "https://a.example.org" and "unknown_key" not in d
    cfg = parse_config(ac.autoconfig(paths, det=NOTHING, write=False)["config"])
    assert cfg.camera.fov_h_deg == 100 and "airplanes.live" in cfg.adsb.url
    paths.setup_dir.mkdir()
    (paths.setup_dir / "settings.json").write_text(json.dumps({"enroll_url": "https://b.example.org"}))
    assert ac.load_defaults(paths)["enroll_url"] == "https://b.example.org"


def test_chrony_conf_follows_pps(paths, tmp_path):
    tdir = ac.chrony_template_dir()
    assert tdir is not None
    assert "refclock PPS /dev/pps1" in ac.chrony_conf(FULL, ac.DEFAULTS, tdir)
    assert "PPS" not in ac.chrony_conf(NOTHING, ac.DEFAULTS, tdir).split("refclock", 1)[1]
    calls = []
    conf = tmp_path / "skynet-gps.conf"
    run = lambda argv, timeout=0, **_: calls.append(argv) or CmdResult(0, "", "")  # noqa: E731
    assert ac.update_chrony(FULL, paths, conf, run) is True
    assert ac.update_chrony(FULL, paths, conf, run) is False  # unchanged: no restart
    assert calls == [["systemctl", "--no-block", "try-restart", "chrony.service"]]


# --- tailscale join -----------------------------------------------------------------------


class FakeTailscale:
    def __init__(self, states, up_rc=0):
        self.states = list(states)
        self.up_rc = up_rc
        self.calls = []
        self.key_seen = None

    def __call__(self, argv, timeout=0, **_):
        self.calls.append(list(argv))
        if argv[:2] == ["tailscale", "status"]:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return CmdResult(0, json.dumps({"BackendState": state}), "")
        if argv[:2] == ["tailscale", "up"]:
            path = next(a for a in argv if a.startswith("--auth-key=")).split("file:", 1)[1]
            self.key_seen = open(path).read()
            self.key_mode = stat.S_IMODE(os.stat(path).st_mode)
            self.key_path = path
            return CmdResult(self.up_rc, "", "" if self.up_rc == 0 else "backend error")
        if argv[:2] == ["tailscale", "version"] or argv[0] == "systemctl":
            return CmdResult(0, "1.90.0", "")
        return CmdResult(127, "", "")


def test_tailscale_join_uses_a_key_file_and_strips_the_key(paths, tmp_path):
    save_credentials(paths.credentials, validate_enrollment(RESP))
    fake = FakeTailscale(["NoState", "NeedsLogin", "Starting", "Running"])
    assert tailscale.join(paths, fake, wait_s=5, sleep=lambda s: None, tmp_root=str(tmp_path)) == 0
    up = next(c for c in fake.calls if c[:2] == ["tailscale", "up"])
    assert fake.key_seen == RESP["tailscale"]["auth_key"] and fake.key_mode == 0o600
    assert not any(RESP["tailscale"]["auth_key"] in a for a in up)
    assert "--advertise-tags=tag:skynet-node" in up and any(a.startswith("--hostname=") for a in up)
    assert not os.path.exists(fake.key_path)
    assert "auth_key" not in json.loads(paths.credentials.read_text())["tailscale"]


def test_tailscale_join_noops_and_failures(paths, tmp_path):
    fake = FakeTailscale(["NeedsLogin"])
    assert tailscale.join(paths, fake, tmp_root=str(tmp_path)) == 0 and fake.calls == []  # not enrolled
    save_credentials(paths.credentials, validate_enrollment({**RESP, "tailscale": None}))
    assert tailscale.join(paths, fake, tmp_root=str(tmp_path)) == 0 and fake.calls == []
    save_credentials(paths.credentials, validate_enrollment(RESP))
    running = FakeTailscale(["Running"])
    assert tailscale.join(paths, running, tmp_root=str(tmp_path)) == 0
    assert ["systemctl", "enable", "--now", "tailscaled.service"] in running.calls
    assert not any(c[:2] == ["tailscale", "up"] for c in running.calls)
    save_credentials(paths.credentials, validate_enrollment(RESP))
    failing = FakeTailscale(["NeedsLogin"], up_rc=1)
    assert tailscale.join(paths, failing, sleep=lambda s: None, tmp_root=str(tmp_path)) == 1
    assert "auth_key" in json.loads(paths.credentials.read_text())["tailscale"]  # kept for the next try
    missing = lambda argv, timeout=0, **_: CmdResult(127, "", "")  # noqa: E731
    assert tailscale.join(paths, missing, tmp_root=str(tmp_path)) == 1
