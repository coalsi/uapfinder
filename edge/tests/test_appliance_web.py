"""The setup page's HTTP handlers: host check, CSRF, auth, enrollment against a fake server, Wi-Fi, actions."""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import stat
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from skynet_edge.appliance import credentials as credmod
from skynet_edge.appliance.nm import NetworkManager
from skynet_edge.appliance.paths import Paths
from skynet_edge.appliance.status import StatusCollector, parse_chrony_sources_csv
from skynet_edge.appliance.util import CmdResult
from skynet_edge.appliance.web.server import SetupApp, make_server

KEY = "cd" * 32
AUTHKEY = "tskey-auth-kSECRET1234-abcdefgh"
PORT = 8341
ENROLL_PORT = 8342


class FakeEnrollServer:
    """Plays the server's POST /api/v1/enroll."""

    def __init__(self, port):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                code = body.get("code")
                if code == "7KQ2M9XD":
                    status, resp = 201, {"node_id": "node-new", "node_key": KEY, "transport": "udp",
                                         "udp_host": "mac-studio", "udp_port": 9999,
                                         "https_base_url": "http://mac-studio:8000",
                                         "tailscale": {"auth_key": AUTHKEY, "tags": ["tag:skynet-node"]}}
                elif code == "G0NE0000":
                    status, resp = 410, {"detail": "used"}
                else:
                    status, resp = 404, {"detail": "unknown"}
                data = json.dumps(resp).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FakeNm(NetworkManager):
    def __init__(self):
        super().__init__(self._run)
        self.calls = []

    def _run(self, argv, timeout=0, **_):
        self.calls.append(list(argv))
        a = argv[1:]
        if "DEVICE,TYPE,STATE,CONNECTION" in a:
            return CmdResult(0, "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:disconnected:\n", "")
        if "list" in a:
            return CmdResult(0, "*:Home:80:WPA2\n :Neighbour:30:WPA1 WPA2\n", "")
        if "connect" in a:
            return CmdResult(0, "Device 'wlan0' successfully activated", "")
        return CmdResult(0, "", "")


@pytest.fixture
def env(tmp_path):
    paths = Paths(tmp_path / "etc", tmp_path / "state", tmp_path / "run")
    for d in (paths.etc, paths.state, paths.run):
        d.mkdir()
    paths.defaults_yaml.write_text(f"enroll_url: http://127.0.0.1:{ENROLL_PORT}\n")
    nm = FakeNm()
    collector = StatusCollector(paths, runner=lambda argv, timeout=0, **_: CmdResult(127, "", ""), nm=nm,
                                gpsd=lambda: {"gpsd": True, "mode": 3, "satellites_used": 9},
                                server_check=lambda creds: {"checked": True, "reachable": True, "online": True})
    app = SetupApp(paths, nm=nm, collector=collector, hostname="uapfinder-1a2b", async_jobs=False)
    srv = make_server(app, "127.0.0.1", PORT)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    fake = FakeEnrollServer(ENROLL_PORT)
    yield app, paths, nm, fake
    srv.shutdown()
    srv.server_close()
    fake.close()


class Client:
    def __init__(self, host="uapfinder.local"):
        self.host = host
        self.cookies = {}

    def req(self, method, path, body=None, headers=None, raw=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        h = {"Host": self.host}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = None
        if body is not None or raw is not None:
            data = raw if raw is not None else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            if csrf and "uapf_csrf" in self.cookies:
                h["X-CSRF-Token"] = self.cookies["uapf_csrf"]
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                value = rest.split(";", 1)[0]
                if value:
                    self.cookies[name] = value
                else:
                    self.cookies.pop(name, None)
        conn.close()
        try:
            js = json.loads(payload)
        except ValueError:
            js = None
        return r.status, js, payload, dict(r.getheaders())

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.req("POST", path, body, **kw)


def test_page_and_security_headers(env):
    c = Client()
    status, _, body, headers = c.get("/")
    assert status == 200 and b"UAP Finder node" in body
    assert "uapf_csrf" in c.cookies and "SameSite=Strict" in headers["Set-Cookie"]
    assert "script-src 'self'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    for name in ("app.js", "app.css", "favicon.svg"):
        assert c.get(f"/static/{name}")[0] == 200
    assert c.get("/static/../server.py")[0] == 404
    assert c.get("/static/%2e%2e/server.py")[0] == 404


@pytest.mark.parametrize("host,ok", [("uapfinder.local", True), ("uapfinder-1a2b.local", True), ("uapfinder-1a2b", True),
                                     ("10.42.0.1", True), ("192.168.1.7:80", True), ("[fe80::1]", True),
                                     ("localhost:80", True), ("evil.example.com", False),
                                     ("uapfinder.local.evil.com", False), ("", False)])
def test_host_allowlist_blocks_dns_rebinding(env, host, ok):
    status = Client(host).get("/api/state")[0]
    assert (status == 200) is ok


def test_csrf_origin_and_content_type(env):
    c = Client()
    c.get("/")
    assert c.post("/api/password", {"password": "longenough"}, csrf=False)[0] == 403
    assert c.post("/api/password", {"password": "longenough"}, headers={"X-CSRF-Token": "x" * 43})[0] == 403
    assert c.post("/api/password", {"password": "longenough"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert c.post("/api/password", {"password": "longenough"}, headers={"Origin": "null"})[0] == 403
    assert c.post("/api/password", {"password": "longenough"}, headers={"Content-Type": "text/plain"})[0] == 415
    assert c.post("/api/password", None, raw=b"{bad")[0] == 400
    assert c.post("/api/password", None, raw=b"x" * 20000)[0] == 413
    assert c.req("OPTIONS", "/api/password")[0] == 405
    assert c.post("/api/password", {"password": "longenough"}, headers={"Origin": "http://uapfinder.local"})[0] == 200


def test_first_password_then_auth_required(env):
    app, paths, nm, _ = env
    c = Client()
    c.get("/")
    st = c.get("/api/state")[1]
    assert st["password_set"] is False and st["authenticated"] is False and st["enrolled"] is False
    assert c.post("/api/password", {"password": "short"})[0] == 400
    status, js, _, headers = c.post("/api/password", {"password": "correct horse"})
    assert status == 200 and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    rec = json.loads((paths.setup_dir / "auth.json").read_text())
    assert rec["algo"] in ("scrypt", "pbkdf2-sha256") and "correct horse" not in json.dumps(rec)
    assert stat.S_IMODE(os.stat(paths.setup_dir / "auth.json").st_mode) == 0o600
    # The login password change was queued for the root helper, readable by its owner only.
    spooled = [p for p in os.listdir(paths.admin_spool) if "set-login-password" in p]
    assert len(spooled) == 1 and stat.S_IMODE(os.stat(paths.admin_spool / spooled[0]).st_mode) == 0o600
    assert c.get("/api/state")[1]["authenticated"] is True
    # Another browser without the session: read-only.
    other = Client()
    other.get("/")
    assert other.get("/api/status")[0] == 200
    assert other.post("/api/enroll", {"code": "7KQ2-M9XD"})[0] == 401
    assert other.post("/api/action", {"action": "reboot"})[0] == 401
    assert other.get("/api/wifi")[0] == 401 and other.get("/api/diagnostics")[0] == 401
    assert other.post("/api/password", {"password": "new password!"})[0] == 401
    assert other.post("/api/login", {"password": "wrong password"})[0] == 401
    assert other.post("/api/login", {"password": "correct horse"})[0] == 200
    assert other.post("/api/action", {"action": "reboot"})[0] == 202
    # Changing the password needs the current one and ends other sessions.
    assert other.post("/api/password", {"password": "new password!", "current": "nope"})[0] == 403
    assert other.post("/api/password", {"password": "new password!", "current": "correct horse"})[0] == 200
    assert c.get("/api/state")[1]["authenticated"] is False
    assert other.post("/api/logout", {})[0] == 200 and other.get("/api/state")[1]["authenticated"] is False


def test_login_rate_limit(env):
    app, *_ = env
    app.passwords.set("correct horse")
    c = Client()
    c.get("/")
    import skynet_edge.appliance.web.server as srvmod

    orig = srvmod.time.sleep
    srvmod.time.sleep = lambda s: None
    try:
        codes = [c.post("/api/login", {"password": f"wrong{i}xxxx"})[0] for i in range(11)]
    finally:
        srvmod.time.sleep = orig
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert c.post("/api/login", {"password": "correct horse"})[0] == 429


def test_enrollment_flow_with_fake_server(env, caplog):
    app, paths, nm, fake = env
    caplog.set_level(logging.DEBUG)
    paths.detect_json.write_text(json.dumps({"platform": {"model": "Jetson", "serial": "1421"},
                                             "camera": {"kind": "csi", "sensor": "imx477"}}))
    c = Client()
    c.get("/")
    status, js, *_ = c.post("/api/enroll", {"code": "nope-nop"})
    assert status == 400 and "8 letters" in js["error"]
    status, js, *_ = c.post("/api/enroll", {"code": "AAAA-BBBB"})
    assert status == 404 and "not known" in js["error"]
    status, js, *_ = c.post("/api/enroll", {"code": "gone-0000"})
    assert status == 410
    status, js, *_ = c.post("/api/enroll", {"code": "7kq2-m9xd"})
    assert status == 201 and js == {"node_id": "node-new", "tailscale": True}
    path, body = fake.requests[-1]
    assert path == "/api/v1/enroll" and body["code"] == "7KQ2M9XD"
    assert body["hardware"]["camera"] == "imx477" and body["hardware"]["serial"] == "1421"
    assert stat.S_IMODE(os.stat(paths.credentials).st_mode) == 0o600
    creds = credmod.load_credentials(paths.credentials)
    assert creds.node_key == KEY and creds.tailscale["auth_key"] == AUTHKEY
    assert c.get("/api/state")[1]["enrolled"] is True
    st = c.get("/api/status")[1]
    assert st["enrolled"] and st["identity"]["node_id"] == "node-new"
    assert KEY not in json.dumps(st) and AUTHKEY not in json.dumps(st)
    assert KEY not in caplog.text and AUTHKEY not in caplog.text


def test_enroll_without_server_configured(env):
    app, paths, *_ = env
    paths.defaults_yaml.write_text("enroll_url: null\n")
    c = Client()
    c.get("/")
    status, js, *_ = c.post("/api/enroll", {"code": "7KQ2-M9XD"})
    assert status == 400 and "no enrollment server" in js["error"]
    assert c.post("/api/settings", {"enroll_url": "ftp://x"})[0] == 400
    assert c.post("/api/settings", {"enroll_url": f"http://127.0.0.1:{ENROLL_PORT}/"})[1] == {
        "enroll_url": f"http://127.0.0.1:{ENROLL_PORT}"}
    assert c.post("/api/enroll", {"code": "7KQ2-M9XD"})[0] == 201


def test_wifi_scan_and_join(env, caplog):
    app, paths, nm, _ = env
    caplog.set_level(logging.DEBUG)
    c = Client()
    c.get("/")
    js = c.get("/api/wifi")[1]
    assert [n["ssid"] for n in js["networks"]] == ["Home", "Neighbour"] and js["device"] == "wlan0"
    status, js, *_ = c.post("/api/wifi", {"ssid": "Home", "password": "s3cret pass; $(reboot)"})
    assert status == 202 and js["state"] == "ok"
    connect = next(a for a in nm.calls if "connect" in a)
    assert connect == ["nmcli", "--wait", "40", "device", "wifi", "connect", "Home", "password",
                       "s3cret pass; $(reboot)", "ifname", "wlan0"]
    assert "s3cret pass" not in caplog.text
    for bad in ({"ssid": "-help"}, {"ssid": ""}, {"ssid": "Home", "password": "short"}, {"ssid": ["x"]},
                {"ssid": "a\nb"}):
        assert c.post("/api/wifi", bad)[0] == 400


def test_actions_go_to_the_admin_spool(env):
    app, paths, *_ = env
    c = Client()
    c.get("/")
    assert c.post("/api/action", {"action": "rm -rf /"})[0] == 400
    for a in ("restart-node", "reboot", "poweroff"):
        assert c.post("/api/action", {"action": a})[0] == 202
    reqs = sorted(os.listdir(paths.admin_spool))
    assert [json.loads((paths.admin_spool / r).read_text())["action"] for r in reqs] == ["restart-node", "reboot",
                                                                                          "poweroff"]


def test_preview_and_status(env):
    app, paths, *_ = env
    c = Client()
    assert c.get("/api/preview.jpg")[0] == 503
    assert paths.preview_request.exists()  # asks the node for snapshots
    paths.preview_jpg.write_bytes(b"\xff\xd8fakejpeg")
    status, _, body, headers = c.get("/api/preview.jpg")
    assert status == 200 and body == b"\xff\xd8fakejpeg" and headers["Content-Type"] == "image/jpeg"
    st = c.get("/api/status")[1]
    assert st["gps"]["mode"] == 3 and st["network"]["connected"] is True
    assert any("Not enrolled" in w for w in st["warnings"])


def test_diagnostics_bundle_is_redacted(env):
    app, paths, *_ = env
    credmod.save_credentials(paths.credentials, credmod.validate_enrollment(
        {"node_id": "node-x", "node_key": KEY, "udp_host": "h", "tailscale": {"auth_key": AUTHKEY, "tags": []}}))
    paths.local_yaml.write_text(f"node:\n  key_hex: {KEY}\nadsb:\n  api_key: hunter2-secret\n")
    c = Client()
    status, _, body, headers = c.get("/api/diagnostics")
    assert status == 200 and headers["Content-Type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(body)) as tar:
        names = tar.getnames()
        blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers())
    assert "uapfinder-diagnostics/identity.json" in names and "uapfinder-diagnostics/edge.local.yaml" in names
    assert KEY.encode() not in blob and AUTHKEY.encode() not in blob and b"hunter2-secret" not in blob


def test_chrony_sources_parsing():
    rows = parse_chrony_sources_csv("#,*,PPS,0,4,377,13,0.000000012,0.000000020,0.000000300\n"
                                    "#,-,NMEA,0,4,377,13,0.03,0.03,0.2\n^,+,ntp.example,2,6,377,40,0.001,0.001,0.01\n")
    assert rows[0] == {"mode": "#", "state": "*", "name": "PPS", "reach": "377"}
