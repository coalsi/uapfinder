"""Star-calibration scheduling, the hotspot logic, nmcli handling, first-boot helpers, the admin spool."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import time
from types import SimpleNamespace

import numpy as np
import pytest

from skynet_edge.appliance import admin, firstboot, netsetup, starcal_job
from skynet_edge.appliance.calibration import load_calibration, save_calibration
from skynet_edge.appliance.nm import HOTSPOT_CON_NAME, NetworkManager, split_terse, validate_psk, validate_ssid
from skynet_edge.appliance.paths import Paths
from skynet_edge.appliance.tap import FrameTap
from skynet_edge.appliance.util import CmdResult, write_json

NOW = 1_790_000_000_000_000_000
DAY = 86400 * 10**9


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path / "etc", tmp_path / "state", tmp_path / "run")
    for d in (p.etc, p.state, p.run):
        d.mkdir()
    return p


# --- star calibration -----------------------------------------------------------------------


class FakeStarcal:
    def __init__(self, dark=True, ok=True, raise_=False):
        self.dark, self.ok, self.raise_ = dark, ok, raise_
        self.calls = []
        self.Observer = lambda lat, lon, alt_m: ("obs", lat, lon, alt_m)
        self.Prior = lambda **kw: ("prior", kw)

    def is_dark_enough(self, t_ns, obs):
        return self.dark

    def calibrate(self, image, t_ns, obs, prior):
        self.calls.append((image.shape, t_ns, obs, prior))
        if self.raise_:
            raise RuntimeError("solver blew up")
        return SimpleNamespace(ok=self.ok, reason="ok" if self.ok else "too few stars", yaw_deg=10.0, pitch_deg=70.0,
                               roll_deg=1.0, fx=1000.0, fy=1000.0, cx=960.0, cy=540.0, k1=-0.2, k2=0.03,
                               stars_matched=40 if self.ok else 3, rms_deg=0.04, t_ns=t_ns)


NODE = {"mode": "running", "camera": {"width": 1920, "height": 1080, "fov_h_deg": 90.0},
        "attitude": {"yaw_deg": 5.0, "pitch_deg": 80.0, "roll_deg": 0.0}, "pointing_source": "prior"}
FIX = {"latitude": 33.6, "longitude": -112.4, "altitude_m": 350.0, "mode": 3, "t_ns": NOW}
CAL = {"version": 1, "ok": True, "t_ns": NOW - 2 * DAY, "width": 1920, "height": 1080, "yaw_deg": 1.0,
       "pitch_deg": 70.0, "roll_deg": 0.0}


@pytest.mark.parametrize("kw,run,reason", [
    (dict(starcal=None), False, "module"),
    (dict(node=None), False, "not running"),
    (dict(node_age_s=120.0), False, "not running"),
    (dict(fix=None), False, "no GPS fix"),
    (dict(fix=dict(FIX, mode=1)), False, "no GPS fix"),
    (dict(fix=dict(FIX, t_ns=NOW - 600 * 10**9)), False, "no GPS fix"),
    (dict(calib=CAL), False, "days old"),
    (dict(starcal=FakeStarcal(dark=False)), False, "not dark"),
    (dict(), True, "dark"),
    (dict(calib=dict(CAL, t_ns=NOW - 8 * DAY)), True, "dark"),
    (dict(calib=CAL, force=True), True, "dark"),
])
def test_decide(kw, run, reason):
    args = dict(now_ns=NOW, node=NODE, node_age_s=3.0, fix=FIX, calib=None, starcal=FakeStarcal())
    args.update(kw)
    d = starcal_job.decide(**args)
    assert d.run is run and reason in d.reason


def fake_stacker(shape=(1080, 1920)):
    def stack(paths):
        return np.zeros(shape, np.float32), {"id": "x", "n": 24, "t_ns": NOW, "width": shape[1], "height": shape[0]}
    return stack


def test_run_job_writes_calibration(paths):
    write_json(paths.node_json, dict(NODE, fix=FIX))
    sc = FakeStarcal()
    out = starcal_job.run_job(paths, starcal=sc, now_ns=NOW, stacker=fake_stacker())
    assert out["ok"] is True and out["stars_matched"] == 40
    cal = load_calibration(paths.calibration)
    assert cal["yaw_deg"] == 10.0 and cal["width"] == 1920 and cal["t_ns"] == NOW and cal["source"] == "starcal"
    obs, prior = sc.calls[0][2], sc.calls[0][3]
    assert obs == ("obs", 33.6, -112.4, 350.0)
    assert prior == ("prior", {"width": 1920, "height": 1080, "fov_h_deg": 90.0})  # heading unknown
    assert json.loads((paths.state / "starcal-last.json").read_text())["ok"] is True
    # Fresh calibration now: the next run skips.
    assert starcal_job.run_job(paths, starcal=sc, now_ns=NOW + 60 * 10**9, stacker=fake_stacker())["attempted"] is False


def test_run_job_imu_prior_and_failures(paths):
    write_json(paths.node_json, dict(NODE, fix=FIX, pointing_source="imu"))
    sc = FakeStarcal(ok=False)
    out = starcal_job.run_job(paths, starcal=sc, now_ns=NOW, stacker=fake_stacker())
    assert out["ok"] is False and "too few" in out["reason"] and not paths.calibration.exists()
    assert sc.calls[0][3][1]["sigma_deg"] == starcal_job.PRIOR_IMU_SIGMA_DEG
    out = starcal_job.run_job(paths, starcal=FakeStarcal(raise_=True), now_ns=NOW, stacker=fake_stacker())
    assert out["ok"] is False and "solver error" in out["reason"]
    out = starcal_job.run_job(paths, starcal=FakeStarcal(), now_ns=NOW, stacker=lambda p: None)
    assert out["ok"] is False and "no frames" in out["reason"]
    save_calibration(paths.calibration, CAL)
    before = paths.calibration.read_text()
    starcal_job.run_job(paths, starcal=FakeStarcal(ok=False), now_ns=NOW, stacker=fake_stacker())
    assert paths.calibration.read_text() == before


def test_run_job_without_fix_in_node_asks_gpsd(paths):
    write_json(paths.node_json, dict(NODE, mode="waiting"))
    out = starcal_job.run_job(paths, starcal=FakeStarcal(), now_ns=NOW, stacker=fake_stacker(),
                              fix_source=lambda: None)
    assert out == {"attempted": False, "reason": "no GPS fix"}
    out = starcal_job.run_job(paths, starcal=FakeStarcal(), now_ns=NOW, stacker=fake_stacker(),
                              fix_source=lambda: dict(FIX))
    assert out["ok"] is True


def test_missing_starcal_module_degrades():
    sc = starcal_job.load_starcal()
    assert sc is None or callable(sc.calibrate)


def test_stack_request_round_trip_through_the_tap(paths):
    tap = FrameTap(str(paths.run))
    clock = {"t": 0.0}
    tap._clock = lambda: clock["t"]
    result = {}

    def node_side(_):
        # The node keeps offering frames while the scheduler waits for the stack.
        for i in range(60):
            clock["t"] += 1.0
            tap.offer(np.full((6, 8), i, np.uint8), NOW + i)

    got = starcal_job.request_stack(paths, frames=4, every=2, timeout_s=5, sleep=node_side)
    assert got is not None
    image, meta = got
    result.update(meta)
    assert image.shape == (6, 8) and image.dtype == np.float32
    assert meta["n"] == 4 and meta["width"] == 8 and meta["t_first_ns"] < meta["t_last_ns"]
    # Frames 0, 2, 4, 6 of the first offering burst were averaged.
    assert float(image[0, 0]) == pytest.approx(np.mean([0, 2, 4, 6]))


def test_preview_written_only_while_requested(paths):
    pytest.importorskip("cv2")
    clock = {"t": 100.0}
    tap = FrameTap(str(paths.run), preview_width=32, clock=lambda: clock["t"])
    y = np.zeros((48, 64), np.uint8)
    tap.offer(y, NOW)
    assert not paths.preview_jpg.exists() and not tap.wants_frames()
    paths.preview_request.touch()
    clock["t"] += 1.0
    assert tap.wants_frames()
    tap.offer(y, NOW)
    assert paths.preview_jpg.read_bytes()[:2] == b"\xff\xd8"
    old = time.time() - 120
    os.utime(paths.preview_request, (old, old))
    clock["t"] += 40.0
    assert not tap.wants_frames()


# --- netsetup ------------------------------------------------------------------------------------


def test_hotspot_decision_timeline():
    st = netsetup.NetState(started=0.0)
    d = lambda t, conn=False, hs=False, saved=0, idle=None: netsetup.decide(t, st, conn, hs, True, saved, idle)  # noqa
    assert d(5) == "none" and d(60) == "none"
    assert d(91) == "start_hotspot"
    assert d(95, hs=True) == "none"
    assert d(200, conn=True, hs=True) == "stop_hotspot"
    assert d(205, conn=True) == "none"
    # Offline again later: another full 90 s grace.
    assert d(300) == "none" and d(380) == "none" and d(391) == "start_hotspot"
    # Saved Wi-Fi + idle setup page: periodic pause so NM can retry.
    assert d(400, hs=True, saved=1, idle=5.0) == "none"
    assert d(391 + 900, hs=True, saved=1, idle=5.0) == "none"  # someone is using the page
    assert d(391 + 901, hs=True, saved=1, idle=700.0) == "pause_hotspot"
    assert d(391 + 930) == "none"  # paused
    assert d(391 + 962) == "start_hotspot"  # pause over, still offline


def test_no_hotspot_without_wifi():
    st = netsetup.NetState(started=0.0)
    assert netsetup.decide(500, st, False, False, False, 0, None) == "none"


def test_hotspot_ssid():
    assert netsetup.hotspot_ssid("uapfinder-1a2b") == "UAPFinder-1A2B"
    assert netsetup.hotspot_ssid("jetson") == "UAPFinder"


def test_split_terse_and_validation():
    assert split_terse(r"My\:Net\\x:80:WPA2") == ["My:Net\\x", "80", "WPA2"]
    assert validate_ssid("Café ☕") == "Café ☕"
    for bad in ("", "-oops", "x" * 33, "a\nb", None, 5):
        with pytest.raises(ValueError):
            validate_ssid(bad)
    assert validate_psk("") is None and validate_psk("ab" * 32) == "ab" * 32
    for bad in ("short", "x" * 64, "pass\x01word", 12345678):
        with pytest.raises(ValueError):
            validate_psk(bad)


class FakeNmcli:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=0, **_):
        self.calls.append(list(argv))
        a = argv[1:]
        if a[:3] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            return CmdResult(0, "eth0:ethernet:unavailable:\nwlan0:wifi:connected:" + HOTSPOT_CON_NAME +
                             "\nlo:loopback:unmanaged:\n", "")
        if "wifi" in a and "list" in a:
            return CmdResult(0, " :Home\\:Net:70:WPA2\n*:Home\\:Net:40:WPA2\n :Cafe:90:\n : :20:WPA2\n", "")
        if a[:2] == ["-t", "-f"] and a[2] == "NAME,TYPE":
            return CmdResult(0, "Home:802-11-wireless\nWired:802-3-ethernet\n" + HOTSPOT_CON_NAME + ":802-11-wireless\n",
                             "")
        return CmdResult(0, "", "")


def test_network_manager_wrappers():
    fake = FakeNmcli()
    nm = NetworkManager(fake)
    devs = nm.devices()
    assert [d.device for d in devs] == ["eth0", "wlan0", "lo"]
    assert nm.is_connected(devs) is False and nm.hotspot_active(devs) is True and nm.wifi_device(devs) == "wlan0"
    nets = nm.wifi_list()
    assert [(n.ssid, n.signal, n.in_use) for n in nets] == [("Home:Net", 70, True), ("Cafe", 90, False)]
    assert nm.saved_wifi_connections() == ["Home"]
    args = nm.connect_args("My Net", "pa ss;$(rm -rf /)", "wlan0")
    assert args == ["--wait", "40", "device", "wifi", "connect", "My Net", "password", "pa ss;$(rm -rf /)", "ifname",
                    "wlan0"]
    assert nm.connect_args("Open", None) == ["--wait", "40", "device", "wifi", "connect", "Open"]
    with pytest.raises(ValueError):
        nm.connect_args("x", "pw", "wlan0; reboot")
    nm.start_hotspot("wlan0", "UAPFinder-1A2B")
    assert ["nmcli", "device", "wifi", "hotspot", "ifname", "wlan0", "con-name", HOTSPOT_CON_NAME, "ssid",
            "UAPFinder-1A2B", "password", "uapfinder"] in fake.calls
    assert ["nmcli", "connection", "modify", HOTSPOT_CON_NAME, "connection.autoconnect", "no"] in fake.calls


class FakeProc:
    def __init__(self, argv, **_):
        self.argv, self.rc, self.terminated = argv, None, False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = 0

    def wait(self, timeout=None):
        return 0


def test_alias_publisher():
    procs = []
    pub = netsetup.AliasPublisher(lambda argv, **kw: procs.append(FakeProc(argv)) or procs[-1])
    pub.update("192.168.1.20")
    assert procs[0].argv == ["avahi-publish", "-a", "-R", "uapfinder.local", "192.168.1.20"]
    pub.update("192.168.1.20")
    assert len(procs) == 1
    pub.update("10.42.0.1")
    assert procs[0].terminated and procs[1].argv[-1] == "10.42.0.1"
    for _ in range(6):  # collision: avahi-publish exits; retried 3 times only
        procs[-1].rc = 1
        pub.update("10.42.0.1")
    assert len(procs) == 5
    pub.update(None)
    assert pub.proc is None


# --- first boot ------------------------------------------------------------------------------------


def w(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def test_hostname_suffix(tmp_path):
    sysr, procr, etcr = str(tmp_path / "sys"), str(tmp_path / "proc"), str(tmp_path / "etc")
    w(f"{etcr}/machine-id", "0123456789abcdef0123456789abcdef\n")
    assert firstboot.hostname_suffix(sysr, procr, etcr) == "cdef"
    w(f"{sysr}/class/net/wlan0/address", "aa:bb:cc:dd:ee:01\n")
    w(f"{sysr}/class/net/wlan0/type", "1\n")
    os.makedirs(f"{sysr}/class/net/wlan0/device")
    os.makedirs(f"{sysr}/class/net/wlan0/wireless")
    w(f"{sysr}/class/net/enP8p1s0/address", "48:b0:2d:12:3a:bc\n")
    w(f"{sysr}/class/net/enP8p1s0/type", "1\n")
    os.makedirs(f"{sysr}/class/net/enP8p1s0/device")
    assert firstboot.hostname_suffix(sysr, procr, etcr) == "3abc"
    w(f"{procr}/device-tree/serial-number", "1421224012345\x00")
    assert firstboot.hostname_suffix(sysr, procr, etcr) == "2345"


def test_root_partition(tmp_path):
    sysr = tmp_path / "sys"
    part = sysr / "devices/platform/mmc/block/mmcblk0/mmcblk0p1"
    w(str(part / "partition"), "1\n")
    (sysr / "dev/block").mkdir(parents=True)
    os.symlink(part, sysr / "dev/block/179:1")
    assert firstboot.root_partition(str(sysr), os.makedev(179, 1)) == ("/dev/mmcblk0", 1, "/dev/mmcblk0p1")


def blk_runner(blkid_rc=2, wipefs_out=""):
    def run(argv, timeout=0, **_):
        if argv[0] == "blkid":
            return CmdResult(blkid_rc, "PTTYPE=gpt\n" if blkid_rc == 0 else "", "")
        if argv[0] == "wipefs":
            return CmdResult(0, wipefs_out, "")
        return CmdResult(127, "", "")
    return run


def test_nvme_blank_rules(tmp_path):
    sysr, procr = str(tmp_path / "sys"), str(tmp_path / "proc")
    w(f"{sysr}/block/nvme0n1/size", "2000000\n")
    os.makedirs(f"{sysr}/block/nvme0n1/holders")
    w(f"{procr}/mounts", "/dev/mmcblk0p1 / ext4 rw 0 0\n")
    zeros = lambda dev, size: (bytes(1 << 20), bytes(1 << 20))  # noqa: E731
    ok, why = firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(), zeros)
    assert ok, why
    assert not firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(blkid_rc=0), zeros)[0]
    assert not firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(blkid_rc=4), zeros)[0]
    assert not firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(wipefs_out="0x438 ext4"), zeros)[0]
    gpt = bytearray(1 << 20)
    gpt[512:520] = b"EFI PART"
    ok, why = firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(), lambda d, s: (bytes(gpt), bytes(16)))
    assert not ok and "GPT" in why
    assert not firstboot.nvme_blank("/dev/sda", sysr, procr, blk_runner(), zeros)[0]
    w(f"{procr}/mounts", "/dev/nvme0n1 /mnt ext4 rw 0 0\n")
    assert not firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(), zeros)[0]
    w(f"{procr}/mounts", "")
    w(f"{sysr}/block/nvme0n1/nvme0n1p1/partition", "1")
    assert firstboot.nvme_blank("/dev/nvme0n1", sysr, procr, blk_runner(), zeros) == (False, "has 1 partition(s)")


def test_raw_signatures():
    head = bytearray(1 << 16)
    head[1080:1082] = b"\x53\xef"
    assert firstboot.raw_signatures(bytes(head), b"") == ["ext2/3/4 superblock"]
    assert firstboot.raw_signatures(bytes(1 << 16), b"xxEFI PARTxx") == ["backup GPT header"]


def test_firstboot_script_passes_bash_syntax_check():
    script = os.path.join(os.path.dirname(__file__), "..", "deploy", "appliance", "uapfinder-firstboot.sh")
    assert subprocess.run(["bash", "-n", script]).returncode == 0


# --- admin spool ----------------------------------------------------------------------------------


def me():
    return pwd.getpwuid(os.getuid()).pw_name


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=0, input_text=None, **_):
        self.calls.append((list(argv), input_text))
        return CmdResult(0, "", "")


def test_admin_spool_actions(paths, monkeypatch):
    monkeypatch.setattr(admin, "LOGIN_USER", me())
    admin.submit(paths, "poweroff")
    time.sleep(0.001)
    admin.submit(paths, "set-login-password", password="correct horse")
    time.sleep(0.001)
    admin.submit(paths, "restart-node")
    rec = Recorder()
    results = admin.process_spool(paths, rec, owner=me())
    argvs = [c[0] for c in rec.calls]
    assert ["chpasswd"] in argvs and ["systemctl", "restart", "--no-block", "skynet-edge.service"] in argvs
    assert argvs[-1] == ["systemctl", "poweroff"]  # power actions run last
    pw_call = next(c for c in rec.calls if c[0] == ["chpasswd"])
    assert pw_call[1] == f"{me()}:correct horse\n"
    assert not any("correct horse" in a for c in rec.calls for a in c[0])
    assert ["systemctl", "enable", "--now", "ssh.service"] in argvs
    assert all(ok for _, ok, _ in results) and os.listdir(paths.admin_spool) == []
    with pytest.raises(ValueError):
        admin.submit(paths, "rm -rf")


def test_admin_spool_rejects_bad_files(paths, tmp_path):
    paths.admin_spool.mkdir(parents=True)
    target = tmp_path / "evil.json"
    target.write_text(json.dumps({"action": "reboot"}))
    os.symlink(target, paths.admin_spool / "1-reboot.json")
    (paths.admin_spool / "2-x.json").write_text(json.dumps({"action": "format-disk"}))
    (paths.admin_spool / "../escape").resolve()
    (paths.admin_spool / "weird name.json").write_text(json.dumps({"action": "reboot"}))
    (paths.admin_spool / "3-big.json").write_text(json.dumps({"action": "reboot", "pad": "x" * 5000}))
    rec = Recorder()
    results = admin.process_spool(paths, rec, owner=me())
    assert rec.calls == [] and len(results) == 4 and not any(ok for _, ok, _ in results)
    # Owned by someone else: refused.
    p = paths.admin_spool / "4-reboot.json"
    p.write_text(json.dumps({"action": "reboot"}))
    req, why = admin.read_request(p, owner_uid=os.getuid() + 1)
    assert req is None and "owned by" in why


def test_admin_password_validation(monkeypatch):
    monkeypatch.setattr(admin, "LOGIN_USER", me())
    rec = Recorder()
    assert admin.perform({"action": "set-login-password", "password": "a:b\ncdefgh"}, rec) == (False, "invalid password")
    assert rec.calls == []


def test_scheduler_with_the_real_star_calibration(paths):
    """End to end on a synthetic night sky: scheduler -> starcal.calibrate -> calibration.json -> autoconfig."""
    sc = starcal_job.load_starcal()
    if sc is None:
        pytest.skip("skynet_edge.starcal not available")
    synth = pytest.importorskip("skynet_edge.starcal.synth")
    from skynet_edge.appliance.autoconfig import autoconfig
    from skynet_edge.config import parse_config

    t_night = 1_792_533_600_000_000_000  # 2026-10-20 22:00 UTC
    lat, lon, alt = 47.0, 8.0, 500.0
    sky = synth.SynthSky(yaw_deg=30.0, pitch_deg=70.0, roll_deg=2.0)
    image = synth.render(sky, t_night, lat, lon, alt)
    write_json(paths.node_json, {"mode": "running", "camera": {"width": 1920, "height": 1080, "fov_h_deg": 90.0},
                                 "pointing_source": "prior",
                                 "fix": {"latitude": lat, "longitude": lon, "altitude_m": alt, "mode": 3,
                                         "t_ns": t_night}})
    out = starcal_job.run_job(paths, starcal=sc, now_ns=t_night,
                              stacker=lambda p: (image, {"t_ns": t_night, "width": 1920, "height": 1080}))
    assert out["ok"] is True, out
    cal = load_calibration(paths.calibration)
    assert abs(((cal["yaw_deg"] - 30.0 + 180) % 360) - 180) < 0.3 and abs(cal["pitch_deg"] - 70.0) < 0.3
    det = {"camera": {"kind": "csi", "sensor": "imx477", "sensor_id": 0}, "imu": {"kind": "none"},
           "gps": {"kind": "gpsd"}, "adsb": {"rtlsdr": False}}
    cfg = parse_config(autoconfig(paths, det=det, write=False)["config"])
    assert cfg.attitude.pointing_source == "starcal" and cfg.camera.intrinsics is not None
    assert cfg.attitude.pointing_sigma_deg < 0.1
