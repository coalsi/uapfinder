"""NetworkManager through ``nmcli``: device state, Wi-Fi scan/join, hotspot.

Every call builds an argument list (no shell) and has a timeout. Values
that come from the setup page (SSID, password) are validated first and are
always passed as *separate* arguments after the keyword nmcli expects
(``... connect <ssid> password <psk>``), so they cannot be read as options.

nmcli's terse output (``-t``) separates fields with ``:`` and escapes a
literal ``:`` or ``\\`` inside a value with a backslash; ``split_terse``
undoes that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .util import CmdResult, run

HOTSPOT_CON_NAME = "UAPFinder-Hotspot"
HOTSPOT_PASSWORD = "uapfinder"

Runner = Callable[..., CmdResult]


class NmError(RuntimeError):
    pass


def split_terse(line: str) -> List[str]:
    """Split one line of ``nmcli -t`` output into fields."""
    fields: List[str] = []
    cur: List[str] = []
    it = iter(line)
    for ch in it:
        if ch == "\\":
            nxt = next(it, "")
            cur.append(nxt)
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def validate_ssid(ssid: object) -> str:
    """An SSID is 1-32 bytes; refuse control characters and a leading '-' (nmcli option syntax)."""
    if not isinstance(ssid, str):
        raise ValueError("network name must be text")
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("network name must be 1-32 bytes")
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("network name contains control characters")
    if ssid.startswith("-"):
        raise ValueError("network names starting with '-' are not supported")
    return ssid


def validate_psk(psk: object) -> Optional[str]:
    """WPA passphrase: 8-63 printable ASCII characters, or 64 hex digits; None/"" for an open network."""
    if psk is None or psk == "":
        return None
    if not isinstance(psk, str):
        raise ValueError("password must be text")
    if re.fullmatch(r"[0-9A-Fa-f]{64}", psk):
        return psk
    if not 8 <= len(psk) <= 63 or any(not (32 <= ord(c) < 127) for c in psk):
        raise ValueError("Wi-Fi password must be 8-63 printable characters")
    return psk


_IFNAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


@dataclass
class Device:
    device: str
    type: str
    state: str
    connection: str


@dataclass
class WifiNetwork:
    ssid: str
    signal: int
    security: str
    in_use: bool


class NetworkManager:
    def __init__(self, runner: Runner = run, nmcli: str = "nmcli"):
        self._run = runner
        self.nmcli = nmcli

    def _cmd(self, args: List[str], timeout: float = 10.0) -> CmdResult:
        return self._run([self.nmcli] + args, timeout=timeout)

    def available(self) -> bool:
        return self._cmd(["-t", "-f", "RUNNING", "general"], timeout=3.0).ok

    def devices(self) -> List[Device]:
        res = self._cmd(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"], timeout=5.0)
        if not res.ok:
            return []
        out = []
        for line in res.stdout.splitlines():
            f = split_terse(line)
            if len(f) >= 4:
                out.append(Device(f[0], f[1], f[2], f[3]))
        return out

    def wifi_device(self, devices: Optional[List[Device]] = None) -> Optional[str]:
        for d in devices if devices is not None else self.devices():
            if d.type == "wifi" and _IFNAME_RE.match(d.device):
                return d.device
        return None

    @staticmethod
    def is_connected(devices: List[Device]) -> bool:
        """True if Ethernet or Wi-Fi (other than our own hotspot) is connected."""
        for d in devices:
            if d.type in ("ethernet", "wifi") and d.state.startswith("connected") and d.connection \
                    and d.connection != HOTSPOT_CON_NAME:
                return True
        return False

    @staticmethod
    def hotspot_active(devices: List[Device]) -> bool:
        return any(d.connection == HOTSPOT_CON_NAME and d.state.startswith("connected") for d in devices)

    def wifi_list(self, rescan: bool = True) -> List[WifiNetwork]:
        args = ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        if rescan:
            args += ["--rescan", "auto"]
        res = self._cmd(args, timeout=20.0)
        if not res.ok:
            raise NmError(res.stderr.strip() or "Wi-Fi scan failed")
        best: Dict[str, WifiNetwork] = {}
        for line in res.stdout.splitlines():
            f = split_terse(line)
            if len(f) < 4 or not f[1].strip() or f[1] == "--":
                continue  # hidden networks have no SSID
            try:
                signal = int(f[2] or 0)
            except ValueError:
                signal = 0
            net = WifiNetwork(f[1], signal, f[3] or "", f[0].strip() == "*")
            prev = best.get(net.ssid)
            if prev is None or net.signal > prev.signal or net.in_use:
                best[net.ssid] = WifiNetwork(net.ssid, max(net.signal, prev.signal if prev else 0), net.security,
                                             net.in_use or bool(prev and prev.in_use))
        return sorted(best.values(), key=lambda n: (-n.in_use, -n.signal, n.ssid))

    def connect_args(self, ssid: str, psk: Optional[str], ifname: Optional[str] = None) -> List[str]:
        """The nmcli arguments for joining a network (exposed for tests)."""
        ssid = validate_ssid(ssid)
        psk = validate_psk(psk)
        args = ["--wait", "40", "device", "wifi", "connect", ssid]
        if psk is not None:
            args += ["password", psk]
        if ifname:
            if not _IFNAME_RE.match(ifname):
                raise ValueError("bad interface name")
            args += ["ifname", ifname]
        return args

    def connect(self, ssid: str, psk: Optional[str], ifname: Optional[str] = None) -> CmdResult:
        args = self.connect_args(ssid, psk, ifname)
        return self._cmd(args, timeout=60.0)

    def saved_wifi_connections(self) -> List[str]:
        res = self._cmd(["-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5.0)
        names = []
        for line in res.stdout.splitlines() if res.ok else []:
            f = split_terse(line)
            if len(f) >= 2 and f[1] in ("802-11-wireless", "wifi") and f[0] != HOTSPOT_CON_NAME:
                names.append(f[0])
        return names

    def hotspot_args(self, ifname: str, ssid: str) -> List[str]:
        if not _IFNAME_RE.match(ifname):
            raise ValueError("bad interface name")
        validate_ssid(ssid)
        return ["device", "wifi", "hotspot", "ifname", ifname, "con-name", HOTSPOT_CON_NAME, "ssid", ssid,
                "password", HOTSPOT_PASSWORD]

    def start_hotspot(self, ifname: str, ssid: str) -> CmdResult:
        res = self._cmd(self.hotspot_args(ifname, ssid), timeout=30.0)
        if res.ok:
            # Never bring the hotspot back by itself after a reboot; netsetup decides.
            self._cmd(["connection", "modify", HOTSPOT_CON_NAME, "connection.autoconnect", "no"], timeout=10.0)
        return res

    def stop_hotspot(self) -> CmdResult:
        return self._cmd(["connection", "down", HOTSPOT_CON_NAME], timeout=20.0)

    def ipv4_addresses(self) -> Dict[str, List[str]]:
        """Device -> IPv4 addresses (without prefix length) of connected devices."""
        out: Dict[str, List[str]] = {}
        for d in self.devices():
            if not d.state.startswith("connected") or d.type not in ("ethernet", "wifi"):
                continue
            res = self._cmd(["-t", "-f", "IP4.ADDRESS", "device", "show", d.device], timeout=5.0)
            for line in res.stdout.splitlines() if res.ok else []:
                f = split_terse(line)
                if len(f) >= 2 and f[1]:
                    out.setdefault(d.device, []).append(f[1].split("/")[0])
        return out
