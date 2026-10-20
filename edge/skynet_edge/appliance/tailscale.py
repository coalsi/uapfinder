"""Join the tailnet with the key from enrollment: ``skynet-edge tailscale-join`` (root).

Runs as ``ExecStartPre=-+`` of ``skynet-edge.service``: as root (the ``+``),
and a failure does not stop the node (the ``-``; the server may also be
reachable without the tailnet).

* Nothing to do without credentials, without a ``tailscale`` block, or when
  ``tailscale status --json`` already reports ``BackendState: Running``.
* The image ships ``tailscaled`` disabled; it is enabled and started here
  (``systemctl enable --now tailscaled.service``) the first time an
  enrollment carries a Tailscale block.
* The auth key is never put on a command line or in a log: it is written to
  a 0600 file in a private 0700 directory under ``/run`` and passed as
  ``tailscale up --auth-key=file:<path>`` (supported by the CLI: "if it
  begins with "file:", then it's a path to a file containing the authkey",
  tailscale/cmd/tailscale/cli/up.go). The file is removed right after.
* ``--advertise-tags`` gets the tags from the server (validated as
  ``tag:name``), ``--hostname`` the node's host name, ``--timeout`` bounds
  the wait; then ``tailscale status --json`` is polled until the backend is
  ``Running``. On success the auth key is deleted from credentials.json.

NOT tested against a real tailnet; the command construction and the flow
are unit-tested with a fake runner.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import tempfile
import time
from typing import Callable, List, Optional

from .credentials import TAG_RE, load_credentials, strip_tailscale_key
from .paths import Paths
from .util import CmdResult, run

log = logging.getLogger(__name__)


def backend_state(runner: Callable[..., CmdResult] = run) -> Optional[str]:
    res = runner(["tailscale", "status", "--json"], timeout=10.0)
    if res.returncode == 127:
        return None
    try:
        return str(json.loads(res.stdout).get("BackendState"))
    except (ValueError, AttributeError):
        return "Unknown"


def up_args(key_file: str, tags: List[str], hostname: str, timeout_s: int = 60) -> List[str]:
    tags = [t for t in tags if TAG_RE.match(t)]
    args = ["tailscale", "up", f"--auth-key=file:{key_file}", f"--hostname={hostname}", f"--timeout={timeout_s}s"]
    if tags:
        args.append(f"--advertise-tags={','.join(tags)}")
    return args


def safe_hostname() -> str:
    name = socket.gethostname().split(".")[0]
    clean = "".join(c if c.isalnum() or c == "-" else "-" for c in name).strip("-")
    return clean[:63] or "uapfinder"


def join(paths: Optional[Paths] = None, runner: Callable[..., CmdResult] = run, wait_s: float = 90.0,
         sleep: Callable[[float], None] = time.sleep, tmp_root: str = "/run") -> int:
    """Returns 0 when nothing was needed or the join worked, 1 on failure."""
    paths = paths or Paths.from_env()
    creds = load_credentials(paths.credentials)
    if creds is None or not creds.tailscale:
        return 0
    if runner(["tailscale", "version"], timeout=10.0).returncode == 127:
        log.warning("tailscale is not installed; skipping the tailnet join")
        return 1
    runner(["systemctl", "enable", "--now", "tailscaled.service"], timeout=60.0)
    state = backend_state(runner)
    for _ in range(10):  # tailscaled may need a moment after starting
        if state not in (None, "Unknown", "NoState"):
            break
        sleep(1.0)
        state = backend_state(runner)
    if state == "Running":
        if creds.tailscale.get("auth_key"):
            strip_tailscale_key(paths.credentials)
        return 0
    key = creds.tailscale.get("auth_key")
    if not key:
        log.warning("not on the tailnet and no auth key left in credentials (tailscale state %s)", state)
        return 1
    tmpdir = tempfile.mkdtemp(prefix="uapf-ts-", dir=tmp_root if os.path.isdir(tmp_root) else None)
    try:
        os.chmod(tmpdir, 0o700)
        key_file = os.path.join(tmpdir, "authkey")
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        log.info("joining the tailnet as %s (tags %s)", safe_hostname(), ",".join(creds.tailscale.get("tags") or []))
        res = runner(up_args(key_file, list(creds.tailscale.get("tags") or []), safe_hostname()), timeout=75.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not res.ok:
        # tailscale's own messages do not contain the key; still keep them short.
        log.error("tailscale up failed (exit %d): %s", res.returncode, res.stderr.strip()[:300])
        return 1
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if backend_state(runner) == "Running":
            strip_tailscale_key(paths.credentials)
            log.info("on the tailnet")
            return 0
        sleep(2.0)
    log.error("tailscale did not reach Running within %.0f s", wait_s)
    return 1
