"""Root-side handler for the setup page's privileged requests: ``skynet-edge admin-requests``.

Least privilege: the setup page runs as ``skynet`` with
``NoNewPrivileges=yes`` and no sudo rights at all. To restart the node,
reboot, shut down or set the login password it drops a small JSON file
into ``/run/skynet/admin/`` (a directory only ``skynet`` and root can
write). ``uapfinder-admin.path`` (``DirectoryNotEmpty=``) starts
``uapfinder-admin.service`` (root, one-shot), which runs this module: each
file is checked (regular file, not a symlink, owned by ``skynet``, small,
a known action), removed, and its action performed. Nothing in a request
is ever interpreted by a shell or used as a command argument, except the
login password, which is fed to ``chpasswd`` on standard input.

Actions: ``restart-node`` (``systemctl restart skynet-edge.service``),
``reboot``, ``poweroff``, ``set-login-password`` (sets the password of the
image's ``uapfinder`` account and enables SSH, which stays off until then).
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .paths import Paths
from .util import CmdResult, run, write_json

log = logging.getLogger(__name__)

ACTIONS = ("restart-node", "reboot", "poweroff", "set-login-password")
LOGIN_USER = "uapfinder"
_NAME_RE = re.compile(r"^[0-9]{1,20}-[a-z-]{1,32}\.json$")
MAX_REQUEST_BYTES = 4096


def submit(paths: Paths, action: str, **fields: Any) -> Path:
    """Queue a request (called by the setup page, as ``skynet``)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    paths.admin_spool.mkdir(parents=True, exist_ok=True)
    p = paths.admin_spool / f"{time.time_ns()}-{action}.json"
    write_json(p, {"action": action, **fields}, mode=0o600)
    return p


def read_request(path: Path, owner_uid: Optional[int]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Open without following symlinks and check the file before trusting a byte of it."""
    if not _NAME_RE.match(path.name):
        return None, "bad file name"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return None, f"cannot open: {exc}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if owner_uid is not None and st.st_uid != owner_uid:
            return None, f"owned by uid {st.st_uid}, expected {owner_uid}"
        if st.st_size > MAX_REQUEST_BYTES:
            return None, "too large"
        raw = os.read(fd, MAX_REQUEST_BYTES + 1)
    finally:
        os.close(fd)
    try:
        req = json.loads(raw)
    except ValueError:
        return None, "not JSON"
    if not isinstance(req, dict) or req.get("action") not in ACTIONS:
        return None, "unknown action"
    return req, "ok"


def perform(req: Dict[str, Any], runner: Callable[..., CmdResult] = run) -> Tuple[bool, str]:
    action = req["action"]
    if action == "restart-node":
        res = runner(["systemctl", "restart", "--no-block", "skynet-edge.service"], timeout=30.0)
    elif action == "reboot":
        res = runner(["systemctl", "reboot"], timeout=30.0)
    elif action == "poweroff":
        res = runner(["systemctl", "poweroff"], timeout=30.0)
    else:
        pw = req.get("password")
        if not isinstance(pw, str) or not 8 <= len(pw) <= 256 or any(c in pw for c in "\n\r:\x00"):
            return False, "invalid password"
        try:
            pwd.getpwnam(LOGIN_USER)
        except KeyError:
            return True, f"no {LOGIN_USER} account on this system; nothing to do"
        res = runner(["chpasswd"], timeout=30.0, input_text=f"{LOGIN_USER}:{pw}\n")
        if res.ok:
            runner(["passwd", "--unlock", LOGIN_USER], timeout=10.0)
            ssh = runner(["systemctl", "enable", "--now", "ssh.service"], timeout=60.0)
            if not ssh.ok:
                log.warning("could not enable ssh: %s", ssh.stderr.strip()[:200])
    if not res.ok:
        return False, (res.stderr.strip() or f"exit {res.returncode}")[:300]
    return True, "done"


def process_spool(paths: Optional[Paths] = None, runner: Callable[..., CmdResult] = run,
                  owner: str = "skynet") -> List[Tuple[str, bool, str]]:
    paths = paths or Paths.from_env()
    try:
        owner_uid: Optional[int] = pwd.getpwnam(owner).pw_uid
    except KeyError:
        owner_uid = None if os.geteuid() != 0 else -1  # as root without a skynet user: accept nothing
    results = []
    try:
        names = sorted(os.listdir(paths.admin_spool))
    except OSError:
        return results
    deferred: List[Dict[str, Any]] = []
    for name in names:
        p = paths.admin_spool / name
        req, why = read_request(p, owner_uid)
        try:
            os.unlink(p)
        except OSError:
            pass
        if req is None:
            log.warning("ignoring admin request %s: %s", name, why)
            results.append((name, False, why))
            continue
        if req["action"] in ("reboot", "poweroff"):
            deferred.append(req)  # after the others, so a password change is not lost
            continue
        ok, msg = perform(req, runner)
        log.info("admin request %s: %s (%s)", req["action"], "ok" if ok else "failed", msg)
        results.append((name, ok, msg))
    for req in deferred[:1]:
        ok, msg = perform(req, runner)
        log.info("admin request %s: %s (%s)", req["action"], "ok" if ok else "failed", msg)
        results.append((req["action"], ok, msg))
    return results
