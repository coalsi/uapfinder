"""The setup and status page: ``skynet-edge setup-server`` (``uapfinder-setup.service``, port 80).

Standard library only (``http.server``); the page is static HTML/CSS/JS
from ``static/`` plus a small JSON API:

========================  ======  ==========================================
path                      auth*   what
========================  ======  ==========================================
GET  /                     -      the page
GET  /api/state            -      password set? logged in? enrolled? + CSRF cookie
GET  /api/status           -      everything the status view shows
GET  /api/preview.jpg      -      latest aiming snapshot (asks the node for one)
GET  /api/wifi             yes    scan results and the last join attempt
POST /api/wifi             yes    join a network ``{"ssid", "password"}``
POST /api/enroll           yes    ``{"code"}``: claim code -> credentials
POST /api/settings         yes    ``{"enroll_url"}`` (advanced)
POST /api/password         yes**  ``{"password", "current"}``
POST /api/login            -      ``{"password"}``
POST /api/logout           -
POST /api/action           yes    ``{"action": "restart-node"|"reboot"|"poweroff"}``
GET  /api/diagnostics      yes    the diagnostics bundle (.tar.gz)
========================  ======  ==========================================

\\* "yes" means: once a device password is set, a valid session is needed;
before that (first setup) the request is allowed. \\*\\* Setting the first
password needs no session; changing it needs the current one.

Protection, because this runs on volunteers' home networks:

* Host allowlist (``uapfinder.local``, ``<hostname>[.local]``, ``localhost``,
  IP literals): defeats DNS rebinding, where a web page on another origin
  resolves its own name to the node.
* State-changing requests need ``Content-Type: application/json``, a
  matching ``Origin`` when the browser sends one, and an ``X-CSRF-Token``
  header equal to the ``uapf_csrf`` cookie (double-submit; the cookie is
  ``SameSite=Strict``). A custom header also forces a CORS preflight that
  this server never approves.
* Session cookie ``HttpOnly; SameSite=Strict``; bodies limited to 16 KiB;
  security headers (CSP without inline script, no framing, no sniffing).
* No shell anywhere; Wi-Fi input is validated (``nm.py``) and passed as
  separate arguments; privileged actions go through the root admin spool
  (``admin.py``). Secrets are never logged.
"""

from __future__ import annotations

import http.cookies
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from ... import __version__
from .. import admin
from ..credentials import (CredentialsError, EnrollError, enroll, hardware_summary, load_credentials,
                           save_credentials, validate_url)
from ..diagnostics import build_bundle
from ..nm import NetworkManager, NmError
from ..paths import Paths
from ..status import StatusCollector
from ..util import file_age_s, read_json, touch, write_json
from .auth import PasswordStore, RateLimiter, Sessions, check_new_password

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {"index.html", "app.css", "app.js", "favicon.svg"}
MAX_BODY = 16 * 1024
CSRF_COOKIE = "uapf_csrf"
SESSION_COOKIE = "uapf_session"
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class SetupApp:
    """State and logic of the page (the HTTP handler below only parses and routes)."""

    def __init__(self, paths: Optional[Paths] = None, nm: Optional[NetworkManager] = None,
                 enroller: Callable[..., Dict[str, Any]] = enroll, collector: Optional[StatusCollector] = None,
                 hostname: Optional[str] = None, async_jobs: bool = True):
        self.paths = paths or Paths.from_env()
        self.nm = nm or NetworkManager()
        self.enroller = enroller
        self.collector = collector or StatusCollector(self.paths, nm=self.nm)
        self.hostname = (hostname or socket.gethostname()).split(".")[0].lower()
        self.passwords = PasswordStore(self.paths.setup_dir / "auth.json")
        self.sessions = Sessions()
        self.login_limit = RateLimiter(per_key=10, total=30, window_s=900.0)
        self.enroll_limit = RateLimiter(per_key=20, total=60, window_s=3600.0)
        self.async_jobs = async_jobs
        self.wifi_job: Dict[str, Any] = {"state": "idle"}
        self._lock = threading.Lock()

    # --- policy --------------------------------------------------------------

    def allowed_host(self, host_header: Optional[str]) -> bool:
        if not host_header:
            return False
        host = host_header.strip()
        if host.startswith("["):  # [IPv6]:port
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        host = host.rstrip(".").lower()
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if not _HOST_RE.match(host):
            return False
        names = {"uapfinder.local", "localhost", self.hostname, f"{self.hostname}.local"}
        return host in names

    def enroll_url(self) -> Optional[str]:
        from ..autoconfig import load_defaults

        url = load_defaults(self.paths).get("enroll_url")
        return url if isinstance(url, str) and url else None

    # --- actions ----------------------------------------------------------------

    def do_enroll(self, code: Any) -> Dict[str, Any]:
        url = self.enroll_url()
        if not url:
            raise CredentialsError("no enrollment server is configured (Advanced > Enrollment server)")
        det = read_json(self.paths.detect_json)
        creds = self.enroller(url, code, hardware_summary(det if isinstance(det, dict) else None, __version__))
        save_credentials(self.paths.credentials, creds)
        log.info("enrolled as node %s via %s", creds["node_id"], url)
        return {"node_id": creds["node_id"], "tailscale": bool(creds.get("tailscale"))}

    def set_enroll_url(self, url: Any) -> str:
        clean = validate_url(url, "enrollment server address")
        p = self.paths.setup_dir / "settings.json"
        cur = read_json(p)
        cur = cur if isinstance(cur, dict) else {}
        cur["enroll_url"] = clean
        write_json(p, cur, mode=0o600)
        return clean

    def start_wifi_join(self, ssid: Any, psk: Any) -> Dict[str, Any]:
        wifi = self.nm.wifi_device()
        args_ok = self.nm.connect_args(ssid, psk, wifi)  # validates; raises ValueError
        del args_ok
        with self._lock:
            if self.wifi_job.get("state") == "running":
                raise ValueError("already joining a network; wait for it to finish")
            self.wifi_job = {"state": "running", "ssid": ssid, "started_ns": time.time_ns()}

        def job() -> None:
            res = self.nm.connect(ssid, psk, wifi)
            with self._lock:
                if res.ok:
                    self.wifi_job = {"state": "ok", "ssid": ssid, "message": f"Connected to {ssid}."}
                else:
                    msg = (res.stderr or res.stdout).strip().splitlines()
                    self.wifi_job = {"state": "failed", "ssid": ssid,
                                     "message": (msg[-1] if msg else f"nmcli exit {res.returncode}")[:200]}
            log.info("Wi-Fi join %s: %s", ssid, self.wifi_job["state"])

        if self.async_jobs:
            threading.Thread(target=job, name="wifi-join", daemon=True).start()
        else:
            job()
        return dict(self.wifi_job)

    def preview(self) -> Optional[bytes]:
        touch(self.paths.preview_request)
        age = file_age_s(self.paths.preview_jpg)
        if age is None or age > 10.0:
            return None
        try:
            return self.paths.preview_jpg.read_bytes()
        except OSError:
            return None


def _json_body(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("body is not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "uapfinder-setup"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: SetupApp  # set on the subclass made by make_server

    # --- plumbing ----------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default, and never a body
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _cookies(self) -> Dict[str, str]:
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return {}
        return {k: m.value for k, m in c.items()}

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None,
              cookies: Tuple[str, ...] = (), cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any, cookies: Tuple[str, ...] = ()) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", cookies=cookies)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _csrf_cookie(self) -> Tuple[str, Tuple[str, ...]]:
        token = self._cookies().get(CSRF_COOKIE, "")
        if re.fullmatch(r"[A-Za-z0-9_-]{32,64}", token):
            return token, ()
        token = secrets.token_urlsafe(32)
        return token, (f"{CSRF_COOKIE}={token}; Path=/; SameSite=Strict",)

    def _session_token(self) -> Optional[str]:
        return self._cookies().get(SESSION_COOKIE)

    def _authenticated(self) -> bool:
        return self.app.sessions.valid(self._session_token())

    def _needs_auth(self) -> bool:
        """True (and a 401 sent) when a password is set and there is no valid session."""
        if self.app.passwords.is_set() and not self._authenticated():
            self._error(401, "log in with the device password first")
            return True
        return False

    def _check_host(self) -> bool:
        if not self.app.allowed_host(self.headers.get("Host")):
            self._error(421, "unexpected Host header; open http://uapfinder.local or the node's IP address")
            return False
        return True

    def _check_post(self) -> Optional[Dict[str, Any]]:
        """CSRF/Origin/Content-Type/size checks; the parsed body, or None after sending an error."""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._error(415, "Content-Type must be application/json")
            return None
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            host = urlsplit(origin).netloc
            if host.lower() != (self.headers.get("Host") or "").lower():
                self._error(403, "cross-origin request refused")
                return None
        elif origin == "null":
            self._error(403, "cross-origin request refused")
            return None
        cookie = self._cookies().get(CSRF_COOKIE, "")
        header = self.headers.get("X-CSRF-Token", "")
        if not cookie or not secrets.compare_digest(cookie.encode(), header.encode()):
            self._error(403, "missing or wrong CSRF token; reload the page")
            return None
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit() or int(length) > MAX_BODY:
            self._error(413, "request body too large")
            return None
        try:
            return _json_body(self.rfile.read(int(length)))
        except ValueError as exc:
            self._error(400, str(exc))
            return None

    # --- dispatch ---------------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        touch(self.app.paths.activity)
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                _, cookies = self._csrf_cookie()
                self._static("index.html", cookies)
            elif path.startswith("/static/") and path[8:] in STATIC_FILES:
                self._static(path[8:])
            elif path == "/favicon.ico":
                self._static("favicon.svg")
            elif path == "/api/state":
                self._state()
            elif path == "/api/status":
                self._json(200, self.app.collector.collect())
            elif path == "/api/preview.jpg":
                img = self.app.preview()
                if img is None:
                    self._error(503, "no preview yet: the node is starting the camera")
                else:
                    self._send(200, img, "image/jpeg")
            elif path == "/api/wifi":
                if self._needs_auth():
                    return
                self._wifi_list()
            elif path == "/api/diagnostics":
                if self._needs_auth():
                    return
                status = self.app.collector.collect()
                data = build_bundle(self.app.paths, status)
                name = f"uapfinder-diagnostics-{self.app.hostname}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
                self._send(200, data, "application/gzip", {"Content-Disposition": f'attachment; filename="{name}"'})
            else:
                self._error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - never leak a traceback to the page
            log.exception("GET %s failed", path)
            self._error(500, "internal error (see the diagnostics bundle)")

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        touch(self.app.paths.activity)
        path = urlsplit(self.path).path
        routes = {"/api/login": self._login, "/api/logout": self._logout, "/api/password": self._password,
                  "/api/enroll": self._enroll, "/api/wifi": self._wifi_join, "/api/settings": self._settings,
                  "/api/action": self._action}
        handler = routes.get(path)
        if handler is None:
            self._error(404, "not found")
            return
        body = self._check_post()
        if body is None:
            return
        if path not in ("/api/login", "/api/logout", "/api/password") and self._needs_auth():
            return
        try:
            handler(body)
        except (ValueError, CredentialsError) as exc:
            self._error(400, str(exc))
        except EnrollError as exc:
            self._error(502 if exc.status is None else (exc.status if exc.status in (404, 410, 429) else 502),
                        str(exc))
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("POST %s failed", path)
            self._error(500, "internal error (see the diagnostics bundle)")

    def do_PUT(self) -> None:  # noqa: N802
        self._error(405, "method not allowed")

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT  # no CORS preflight is ever approved

    # --- GET handlers -------------------------------------------------------------------

    def _static(self, name: str, cookies: Tuple[str, ...] = ()) -> None:
        p = STATIC_DIR / name
        try:
            data = p.read_bytes()
        except OSError:
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype, cookies=cookies, cache="no-cache")

    def _state(self) -> None:
        token, cookies = self._csrf_cookie()
        app = self.app
        creds = load_credentials(app.paths.credentials)
        self._json(200, {"password_set": app.passwords.is_set(), "authenticated": self._authenticated(),
                         "enrolled": creds is not None, "node_id": creds.node_id if creds else None,
                         "enroll_url": app.enroll_url(), "hostname": app.hostname, "version": __version__,
                         "csrf": token}, cookies)

    def _wifi_list(self) -> None:
        try:
            nets = self.app.nm.wifi_list()
            err = None
        except NmError as exc:
            nets, err = [], str(exc)[:200]
        self._json(200, {"networks": [n.__dict__ for n in nets[:40]], "error": err, "job": self.app.wifi_job,
                         "device": self.app.nm.wifi_device()})

    # --- POST handlers ----------------------------------------------------------------------

    def _client(self) -> str:
        return self.client_address[0]

    def _login(self, body: Dict[str, Any]) -> None:
        app = self.app
        if not app.passwords.is_set():
            self._error(409, "no device password is set yet")
            return
        if app.login_limit.blocked(self._client()):
            self._error(429, "too many failed attempts; wait 15 minutes")
            return
        if not app.passwords.verify(body.get("password", "")):
            app.login_limit.record(self._client())
            time.sleep(0.5)
            self._error(401, "wrong password")
            return
        token = app.sessions.create()
        self._json(200, {"ok": True}, (self._session_cookie(token),))

    def _session_cookie(self, token: str) -> str:
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(12 * 3600)}"

    def _logout(self, body: Dict[str, Any]) -> None:
        self.app.sessions.revoke(self._session_token())
        self._json(200, {"ok": True}, (f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",))

    def _password(self, body: Dict[str, Any]) -> None:
        app = self.app
        new = check_new_password(body.get("password"))
        if app.passwords.is_set():
            if not self._authenticated():
                self._error(401, "log in with the device password first")
                return
            if app.login_limit.blocked(self._client()):
                self._error(429, "too many failed attempts; wait 15 minutes")
                return
            if not app.passwords.verify(body.get("current", "")):
                app.login_limit.record(self._client())
                self._error(403, "the current password is wrong")
                return
        app.passwords.set(new)
        app.sessions.revoke_all()
        try:
            admin.submit(app.paths, "set-login-password", password=new)
        except OSError as exc:
            log.warning("could not queue the login password update: %s", exc)
        token = app.sessions.create()
        self._json(200, {"ok": True}, (self._session_cookie(token),))

    def _enroll(self, body: Dict[str, Any]) -> None:
        app = self.app
        if app.enroll_limit.blocked(self._client()):
            self._error(429, "too many attempts; wait an hour")
            return
        app.enroll_limit.record(self._client())
        self._json(201, app.do_enroll(body.get("code")))

    def _wifi_join(self, body: Dict[str, Any]) -> None:
        job = self.app.start_wifi_join(body.get("ssid"), body.get("password") or None)
        self._json(202, job)

    def _settings(self, body: Dict[str, Any]) -> None:
        self._json(200, {"enroll_url": self.app.set_enroll_url(body.get("enroll_url"))})

    def _action(self, body: Dict[str, Any]) -> None:
        action = body.get("action")
        if action not in ("restart-node", "reboot", "poweroff"):
            raise ValueError("unknown action")
        admin.submit(self.app.paths, action)
        log.info("action %s requested from %s", action, self._client())
        self._json(202, {"ok": True, "action": action})


def make_server(app: SetupApp, bind: str = "0.0.0.0", port: int = 80) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((bind, port), handler)
    srv.daemon_threads = True
    return srv


def serve(bind: str = "0.0.0.0", port: int = 80, paths: Optional[Paths] = None) -> int:
    app = SetupApp(paths)
    for d in (app.paths.setup_dir,):
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        except OSError:
            pass
    srv = make_server(app, bind, port)
    log.info("setup page on http://%s:%d (also http://uapfinder.local)", bind, port)
    import signal

    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
