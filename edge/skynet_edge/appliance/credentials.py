"""The node's identity (``/var/lib/skynet/credentials.json``) and claim-code enrollment.

``credentials.json`` (mode 0600, owner ``skynet``) holds the server's
``POST /api/v1/enroll`` response, validated, plus where and when it was
obtained::

    {"node_id": "...", "node_key": "<64 hex>", "transport": "udp", "udp_host": "...", "udp_port": 9999,
     "https_base_url": "http://...", "tailscale": {"auth_key": "tskey-auth-...", "tags": ["tag:skynet-node"]},
     "enroll_url": "https://...", "enrolled_ns": 1790000000000000000}

After the node has joined the tailnet the auth key is removed from the file
(``tailscale.joined: true`` stays), so the reusable secret does not linger.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .util import read_json, write_json

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_MAP = {"O": "0", "I": "1", "L": "1"}
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*\.?$")
IPV6_RE = re.compile(r"^[0-9A-Fa-f:]{2,39}$")
TAG_RE = re.compile(r"^tag:[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
AUTHKEY_RE = re.compile(r"^tskey-[A-Za-z0-9_-]{8,200}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]+(:\d{1,5})?(/[A-Za-z0-9._~%/-]*)?$")


class CredentialsError(ValueError):
    pass


def normalize_code(code: object) -> str:
    """``7kq2-m9xd`` -> ``7KQ2M9XD``. Case-insensitive, dashes/spaces ignored, O->0 and I/L->1 (Crockford)."""
    if not isinstance(code, str):
        raise CredentialsError("enter the claim code (8 letters and digits)")
    s = re.sub(r"[\s-]", "", code).upper()
    s = "".join(_CODE_MAP.get(c, c) for c in s)
    if len(s) != 8 or any(c not in CROCKFORD for c in s):
        raise CredentialsError("a claim code is 8 letters and digits, like 7KQ2-M9XD")
    return s


def format_code(code: str) -> str:
    c = normalize_code(code)
    return f"{c[:4]}-{c[4:]}"


def validate_url(url: object, what: str = "URL") -> str:
    if not isinstance(url, str) or not URL_RE.match(url) or len(url) > 300:
        raise CredentialsError(f"{what} must be an http:// or https:// address")
    return url.rstrip("/")


def _host(v: object) -> str:
    if not isinstance(v, str) or not (HOST_RE.match(v) or IPV6_RE.match(v)):
        raise CredentialsError("server sent an invalid udp_host")
    return v


def validate_enrollment(resp: Any) -> Dict[str, Any]:
    """Check a ``POST /api/v1/enroll`` response field by field; returns the cleaned record."""
    if not isinstance(resp, dict):
        raise CredentialsError("server response is not a JSON object")
    node_id, key = resp.get("node_id"), resp.get("node_key")
    if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
        raise CredentialsError("server sent an invalid node_id")
    if not isinstance(key, str) or not KEY_RE.match(key.lower()):
        raise CredentialsError("server sent an invalid node_key")
    transport = resp.get("transport", "udp")
    if transport not in ("udp", "https"):
        raise CredentialsError("server sent an unknown transport")
    out: Dict[str, Any] = {"node_id": node_id, "node_key": key.lower(), "transport": transport}
    if resp.get("udp_host") is not None:
        out["udp_host"] = _host(resp["udp_host"])
    port = resp.get("udp_port", 9999)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise CredentialsError("server sent an invalid udp_port")
    out["udp_port"] = port
    if resp.get("https_base_url") is not None:
        out["https_base_url"] = validate_url(resp["https_base_url"], "https_base_url")
    if transport == "udp" and "udp_host" not in out:
        raise CredentialsError("server chose UDP but sent no udp_host")
    if transport == "https" and "https_base_url" not in out:
        raise CredentialsError("server chose HTTPS but sent no https_base_url")
    ts = resp.get("tailscale")
    if ts is not None:
        if not isinstance(ts, dict):
            raise CredentialsError("server sent an invalid tailscale block")
        tags = ts.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) and TAG_RE.match(t) for t in tags) \
                or len(tags) > 10:
            raise CredentialsError("server sent invalid tailscale tags")
        block: Dict[str, Any] = {"tags": list(tags)}
        auth = ts.get("auth_key")
        if auth is not None:
            if not isinstance(auth, str) or not AUTHKEY_RE.match(auth):
                raise CredentialsError("server sent an invalid tailscale auth key")
            block["auth_key"] = auth
        if ts.get("joined") is True:
            block["joined"] = True
        out["tailscale"] = block
    else:
        out["tailscale"] = None
    return out


@dataclass
class Credentials:
    data: Dict[str, Any]

    @property
    def node_id(self) -> str:
        return str(self.data["node_id"])

    @property
    def node_key(self) -> str:
        return str(self.data["node_key"])

    @property
    def tailscale(self) -> Optional[Dict[str, Any]]:
        return self.data.get("tailscale")

    def public(self) -> Dict[str, Any]:
        """Everything except secrets (for the status page and diagnostics)."""
        d = {k: v for k, v in self.data.items() if k != "node_key"}
        if d.get("tailscale"):
            d["tailscale"] = {k: v for k, v in d["tailscale"].items() if k != "auth_key"}
            d["tailscale"]["auth_key_present"] = bool(self.data["tailscale"].get("auth_key"))
        return d


def load_credentials(path: Union[str, Path]) -> Optional[Credentials]:
    """Validated credentials, or None if the file is missing or not valid."""
    raw = read_json(path)
    if raw is None:
        return None
    try:
        clean = validate_enrollment(raw)
    except CredentialsError:
        return None
    for extra in ("enroll_url", "enrolled_ns", "label"):
        if extra in raw:
            clean[extra] = raw[extra]
    return Credentials(clean)


def save_credentials(path: Union[str, Path], data: Dict[str, Any]) -> None:
    write_json(path, data, mode=0o600)


def strip_tailscale_key(path: Union[str, Path]) -> bool:
    """Remove the tailscale auth key after a successful join; True if the file changed."""
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("tailscale"), dict):
        return False
    ts = dict(raw["tailscale"])
    if "auth_key" not in ts and ts.get("joined"):
        return False
    ts.pop("auth_key", None)
    ts["joined"] = True
    raw["tailscale"] = ts
    # Keep the owner and mode of the original file (this runs as root for a skynet-owned file).
    st = os.stat(path)
    save_credentials(path, raw)
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass
    return True


# --- enrollment client ------------------------------------------------------------


ENROLL_ERRORS = {
    404: "That claim code is not known to the server. Check it against the message from your coordinator.",
    410: "That claim code has already been used or has expired. Ask your coordinator for a new one.",
    429: "Too many attempts from this network. Wait an hour and try again.",
    503: "The server cannot enroll nodes right now. Try again later or tell your coordinator.",
}


class EnrollError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def enroll(enroll_url: str, code: str, hardware: Dict[str, Any], session: Any = None,
           timeout_s: float = 20.0) -> Dict[str, Any]:
    """Exchange a claim code for credentials (does not store them)."""
    import requests

    base = validate_url(enroll_url, "enrollment server address")
    body = {"code": normalize_code(code), "hardware": hardware}
    http = session or requests
    try:
        resp = http.post(f"{base}/api/v1/enroll", json=body, timeout=timeout_s)
    except requests.RequestException as exc:
        raise EnrollError(f"Could not reach the enrollment server ({type(exc).__name__}). "
                          "Check the node's network connection.") from None
    if resp.status_code != 201:
        msg = ENROLL_ERRORS.get(resp.status_code)
        if msg is None:
            msg = f"The enrollment server answered {resp.status_code}."
            if resp.status_code == 422:
                msg = "The enrollment server rejected the request (422)."
        raise EnrollError(msg, resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        raise EnrollError("The enrollment server sent an invalid response.") from None
    try:
        clean = validate_enrollment(data)
    except CredentialsError as exc:
        raise EnrollError(f"The enrollment server sent an invalid response: {exc}.") from None
    clean["enroll_url"] = base
    clean["enrolled_ns"] = time.time_ns()
    return clean


def hardware_summary(det: Optional[Dict[str, Any]], version: str) -> Dict[str, Any]:
    """The ``hardware`` member of the enroll request, from a detect result."""
    det = det or {}
    plat = det.get("platform") or {}
    cam = det.get("camera") or {}
    gps = det.get("gps") or {}
    pps = det.get("pps") or {}
    camera = cam.get("sensor") if cam.get("kind") == "csi" else cam.get("kind", "none")
    gps_kind = "none"
    if gps.get("kind") not in (None, "none"):
        gps_kind = "pps" if pps.get("device") else ("usb" if gps.get("usb") else "serial")
    serial = plat.get("serial")
    out: Dict[str, Any] = {"model": (plat.get("model") or "unknown")[:100], "camera": str(camera or "none")[:40],
                           "gps": gps_kind, "software_version": version}
    if serial:
        out["serial"] = str(serial)[:64]
    extras: List[str] = []
    if (det.get("imu") or {}).get("kind") == "bno055":
        extras.append("bno055")
    if (det.get("adsb") or {}).get("rtlsdr"):
        extras.append("rtlsdr")
    if det.get("nvme"):
        extras.append("nvme")
    if extras:
        out["extras"] = extras
    return out


def credentials_json_text(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)
