"""Claim-code enrollment of nodes (docs/NODE_APPLIANCE.md "Enrollment", docs/API.md).

* Codes are 8 characters of Crockford base32 (``0-9 A-Z`` without I, L, O,
  U; 40 bits from ``secrets``), shown as ``XXXX-XXXX``. Input is
  case-insensitive, ignores dashes and spaces, and maps O to 0 and I/L to 1
  as Crockford's spec suggests.
* ``POST /api/v1/enroll`` is public and rate-limited per client address
  (``SKYNET_ENROLL_RATE_PER_HOUR``, default 10). With 2^40 codes, guessing is
  hopeless at that rate.
* The response tells the node how to reach the server: the transport
  (``SKYNET_NODE_TRANSPORT``, default ``udp`` on a standalone server with UDP
  enabled, else ``https``), the UDP endpoint (``SKYNET_NODE_UDP_HOST`` /
  ``SKYNET_NODE_UDP_PORT``, defaulting to ``SKYNET_UDP_PUBLIC_ENDPOINT``, then
  the host of ``SKYNET_PUBLIC_URL``, then the host the request was sent to)
  and the HTTP base URL (``SKYNET_PUBLIC_URL`` or the request's base URL),
  plus a Tailscale auth key when ``SKYNET_TAILSCALE_AUTHKEY`` is set.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ALIASES = {"O": "0", "I": "1", "L": "1"}
MAX_BODY_BYTES = 4096
RATE_WINDOW_S = 3600.0


def new_code() -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(8))


def normalize_code(code: Any) -> Optional[str]:
    """The canonical 8-character code, or None if it cannot be one."""
    if not isinstance(code, str) or len(code) > 32:
        return None
    s = re.sub(r"[\s-]", "", code).upper()
    s = "".join(_ALIASES.get(c, c) for c in s)
    if len(s) != 8 or any(c not in CROCKFORD for c in s):
        return None
    return s


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


class EnrollmentsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=1, ge=1, le=200)
    label: Optional[str] = Field(default=None, max_length=200)
    expires_in_h: float = Field(default=720.0, gt=0, le=24 * 366)


class Hardware(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    serial: Optional[str] = Field(default=None, max_length=64)
    model: Optional[str] = Field(default=None, max_length=100)
    camera: Optional[str] = Field(default=None, max_length=40)
    gps: Optional[str] = Field(default=None, max_length=20)
    software_version: Optional[str] = Field(default=None, max_length=40)
    extras: List[str] = Field(default_factory=list, max_length=10)

    @field_validator("extras")
    @classmethod
    def _short(cls, v: List[str]) -> List[str]:
        return [str(x)[:32] for x in v]


class EnrollIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: str = Field(max_length=32)
    hardware: Hardware = Field(default_factory=Hardware)


def node_label(label: Optional[str], hw: Hardware) -> Optional[str]:
    parts = [p for p in (label, hw.serial and f"serial {hw.serial}") if p]
    return " · ".join(parts)[:200] if parts else None


def connection_info(settings: Any, request_host: Optional[str], request_base_url: str) -> Dict[str, Any]:
    """transport / udp_host / udp_port / https_base_url for an enrolled node."""
    public = settings.public_url
    udp_host = settings.node_udp_host
    udp_port = settings.node_udp_port
    endpoint = settings.udp_public_endpoint
    if endpoint and (udp_host is None or udp_port is None):
        h, _, p = endpoint.rpartition(":")
        if h and p.isdigit():
            udp_host = udp_host or h.strip("[]")
            udp_port = udp_port or int(p)
    if udp_host is None and public:
        udp_host = urlsplit(public).hostname
    if udp_host is None:
        udp_host = request_host
    if udp_port is None:
        udp_port = settings.udp_port or 9999
    transport = settings.node_transport
    if transport is None:
        transport = "udp" if settings.udp_enabled else "https"
    return {"transport": transport, "udp_host": udp_host, "udp_port": int(udp_port),
            "https_base_url": (public or request_base_url).rstrip("/")}


def tailscale_block(settings: Any) -> Optional[Dict[str, Any]]:
    if not settings.tailscale_authkey:
        return None
    return {"auth_key": settings.tailscale_authkey, "tags": list(settings.tailscale_tags)}
