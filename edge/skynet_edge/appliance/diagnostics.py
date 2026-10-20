"""The diagnostics bundle (``.tar.gz``) offered by the setup page.

Contents: detection and status JSON, the generated and local configuration,
image defaults, the identity without its key, calibration files, chrony and
network state, disk usage and the last 24 h of the node services' journal.
Every text file passes ``util.redact`` (Tailscale keys, 64-hex node keys,
``password``/``key``-named values) and JSON files ``util.redact_obj``.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .credentials import load_credentials
from .paths import Paths
from .util import CmdResult, read_json, read_text, redact, redact_obj, run

UNITS = ("skynet-edge.service", "uapfinder-setup.service", "uapfinder-netsetup.service",
         "uapfinder-firstboot.service", "uapfinder-starcal.service", "uapfinder-admin.service", "gpsd.service",
         "chrony.service", "readsb.service", "tailscaled.service")
MAX_LOG_BYTES = 8 * 1024 * 1024


def _commands() -> List[Tuple[str, List[str], float]]:
    journal = ["journalctl", "--no-pager", "-o", "short-iso", "--since", "-24h", "-n", "20000"]
    for u in UNITS:
        journal += ["-u", u]
    return [
        ("journal.txt", journal, 20.0),
        ("chrony-tracking.txt", ["chronyc", "tracking"], 3.0),
        ("chrony-sources.txt", ["chronyc", "sources", "-v"], 3.0),
        ("nmcli-devices.txt", ["nmcli", "device", "status"], 5.0),
        ("df.txt", ["df", "-h"], 5.0),
        ("lsusb.txt", ["lsusb"], 5.0),
        ("uname.txt", ["uname", "-a"], 2.0),
        ("systemctl-failed.txt", ["systemctl", "--failed", "--no-pager"], 5.0),
    ]


def build_bundle(paths: Paths, status: Optional[Dict[str, Any]] = None,
                 runner: Callable[..., CmdResult] = run, now: Optional[float] = None) -> bytes:
    files: Dict[str, bytes] = {}

    def add_text(name: str, text: Optional[str]) -> None:
        if text is not None:
            files[name] = redact(text).encode("utf-8")[:MAX_LOG_BYTES]

    def add_json(name: str, obj: Any) -> None:
        if obj is not None:
            files[name] = json.dumps(redact_obj(obj), indent=2, sort_keys=True).encode("utf-8")

    add_json("status.json", status)
    add_json("detect.json", read_json(paths.detect_json))
    add_json("autoconfig.json", read_json(paths.run / "autoconfig.json"))
    add_json("node.json", read_json(paths.node_json))
    add_json("calibration.json", read_json(paths.calibration))
    add_json("starcal-last.json", read_json(paths.state / "starcal-last.json"))
    creds = load_credentials(paths.credentials)
    add_json("identity.json", creds.public() if creds else {"enrolled": False})
    for name, p in (("edge.yaml", paths.edge_yaml), ("edge.local.yaml", paths.local_yaml),
                    ("defaults.yaml", paths.defaults_yaml)):
        add_text(name, read_text(p, 1 << 20))
    for name, argv, timeout in _commands():
        res = runner(argv, timeout=timeout)
        add_text(name, res.stdout + (f"\n[exit {res.returncode}] {res.stderr}" if not res.ok else ""))
    buf = io.BytesIO()
    mtime = time.time() if now is None else now
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(f"uapfinder-diagnostics/{name}")
            info.size = len(data)
            info.mtime = int(mtime)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
