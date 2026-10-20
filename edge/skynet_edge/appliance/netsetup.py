"""``skynet-edge netsetup`` (``uapfinder-netsetup.service``, root): fallback hotspot and the mDNS alias.

Hotspot: if no Ethernet or Wi-Fi connection exists 90 s after the service
started, start ``nmcli device wifi hotspot`` as ``UAPFinder-XXXX`` (WPA2,
password ``uapfinder``). NetworkManager's ``shared`` IPv4 method gives the
node 10.42.0.1 and runs DHCP for the phone. The hotspot is taken down as
soon as another connection is up (e.g. Ethernet plugged in, or the setup
page joined a Wi-Fi network, which itself replaces the hotspot on the same
radio). If saved Wi-Fi networks exist and nobody has used the setup page for
10 minutes, the hotspot is dropped for 60 s every 15 minutes so
NetworkManager can retry them (a home router that was off when the node
booted).

mDNS: avahi publishes ``<hostname>.local`` itself. ``uapfinder.local`` is an
extra name, published with ``avahi-publish -a -R uapfinder.local <ipv4>``
(``-R``: no reverse PTR, so the host's own name stays the reverse entry) for
the current primary IPv4 address, restarted when the address changes. If
another node on the network already holds ``uapfinder.local``, avahi reports
a collision and this node is reachable as ``uapfinder-XXXX.local``.

NOT hardware-tested (hotspot behaviour depends on the Wi-Fi driver); the
decision logic is unit-tested.
"""

from __future__ import annotations

import logging
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .nm import NetworkManager
from .paths import Paths
from .util import file_age_s

log = logging.getLogger(__name__)

OFFLINE_GRACE_S = 90.0
RETRY_IDLE_S = 600.0
RETRY_EVERY_S = 900.0
RETRY_PAUSE_S = 60.0
ALIAS = "uapfinder.local"


@dataclass
class NetState:
    """What the loop remembers between ticks (monotonic seconds)."""

    started: float
    offline_since: Optional[float] = None
    hotspot_since: Optional[float] = None
    paused_until: Optional[float] = None
    was_connected: bool = False


def decide(now: float, st: NetState, connected: bool, hotspot: bool, has_wifi: bool, saved_wifi: int,
           setup_idle_s: Optional[float]) -> str:
    """One of ``none``, ``start_hotspot``, ``stop_hotspot``, ``pause_hotspot``. Updates ``st``."""
    if connected:
        st.was_connected = True
        st.offline_since = None
        st.paused_until = None
        if hotspot:
            return "stop_hotspot"
        st.hotspot_since = None
        return "none"
    if st.offline_since is None:
        st.offline_since = now if st.was_connected else st.started  # offline since boot
    if hotspot:
        if st.hotspot_since is None:
            st.hotspot_since = now
        idle = setup_idle_s is None or setup_idle_s >= RETRY_IDLE_S
        if saved_wifi and idle and now - st.hotspot_since >= RETRY_EVERY_S:
            st.paused_until = now + RETRY_PAUSE_S
            st.hotspot_since = None
            return "pause_hotspot"
        return "none"
    if not has_wifi:
        return "none"
    if st.paused_until is not None and now < st.paused_until:
        return "none"
    offline_for = now - max(st.offline_since, st.started)
    if now - st.started >= OFFLINE_GRACE_S and offline_for >= (0.0 if st.paused_until else OFFLINE_GRACE_S):
        st.paused_until = None
        st.hotspot_since = now
        return "start_hotspot"
    return "none"


def hotspot_ssid(hostname: Optional[str] = None) -> str:
    """``UAPFinder-XXXX`` from the ``uapfinder-xxxx`` host name (XXXX as in the host name)."""
    name = (hostname or socket.gethostname()).split(".")[0]
    suffix = name.rsplit("-", 1)[-1] if name.lower().startswith("uapfinder-") else ""
    suffix = "".join(c for c in suffix if c.isalnum())[:8].upper()
    return f"UAPFinder-{suffix}" if suffix else "UAPFinder"


def primary_ipv4(nm: NetworkManager) -> Optional[str]:
    addrs = nm.ipv4_addresses()
    for dev in sorted(addrs, key=lambda d: (not d.startswith(("e", "en")), d)):
        for a in addrs[dev]:
            if not a.startswith("169.254."):
                return a
    return None


class AliasPublisher:
    """Keeps one ``avahi-publish -a -R uapfinder.local <ip>`` running for the current address."""

    def __init__(self, popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self._popen = popen
        self.proc: Optional[subprocess.Popen] = None
        self.ip: Optional[str] = None
        self._failures = 0

    def update(self, ip: Optional[str]) -> None:
        """Publish for ``ip``; a publisher that keeps dying (a name collision) is retried 3 times."""
        if ip != self.ip:
            self.stop()
            self.ip = ip
            self._failures = 0
            if ip is not None:
                self._start(ip)
            return
        alive = self.proc is not None and self.proc.poll() is None
        if ip is None or alive or self._failures >= 3:
            return
        self._failures += 1
        self._start(ip)

    def _start(self, ip: str) -> None:
        try:
            self.proc = self._popen(["avahi-publish", "-a", "-R", ALIAS, ip], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            log.info("publishing %s -> %s", ALIAS, ip)
        except OSError as exc:
            log.warning("avahi-publish: %s", exc)
            self.proc = None
            self._failures += 1

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def loop(stop: threading.Event, paths: Optional[Paths] = None, nm: Optional[NetworkManager] = None,
         interval_s: float = 5.0, alias: bool = True) -> None:
    paths = paths or Paths.from_env()
    nm = nm or NetworkManager()
    st = NetState(started=time.monotonic())
    pub = AliasPublisher() if alias else None
    ssid = hotspot_ssid()
    saved_cache: List[str] = []
    next_saved = 0.0
    try:
        while not stop.is_set():
            now = time.monotonic()
            devs = nm.devices()
            wifi = nm.wifi_device(devs)
            if now >= next_saved:
                saved_cache = nm.saved_wifi_connections()
                next_saved = now + 60.0
            action = decide(now, st, nm.is_connected(devs), nm.hotspot_active(devs), wifi is not None,
                            len(saved_cache), file_age_s(paths.activity))
            if action == "start_hotspot" and wifi:
                log.info("no network for %.0f s: starting hotspot %s on %s", now - st.started, ssid, wifi)
                res = nm.start_hotspot(wifi, ssid)
                if not res.ok:
                    log.error("hotspot failed: %s", res.stderr.strip()[:300])
            elif action == "stop_hotspot":
                log.info("network connected: stopping the hotspot")
                nm.stop_hotspot()
            elif action == "pause_hotspot":
                log.info("pausing the hotspot for %.0f s so saved Wi-Fi networks can be retried", RETRY_PAUSE_S)
                nm.stop_hotspot()
            if pub is not None:
                pub.update(primary_ipv4(nm))
            stop.wait(interval_s)
    finally:
        if pub is not None:
            pub.stop()


def main_loop() -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    loop(stop)
    return 0
