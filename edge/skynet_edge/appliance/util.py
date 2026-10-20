"""Small helpers shared by the appliance modules: commands, atomic files, redaction."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Union

log = logging.getLogger(__name__)


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[..., CmdResult]


def run(argv: Sequence[str], timeout: float = 5.0, input_text: Optional[str] = None,
        env: Optional[Dict[str, str]] = None) -> CmdResult:
    """Run a command (argument list, never a shell) with a hard timeout.

    Returns returncode 127 when the program is missing and 124 on timeout,
    like coreutils ``timeout``; never raises for those.
    """
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, input=input_text,
                              env=env, check=False)
    except FileNotFoundError:
        return CmdResult(127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"{argv[0]}: timed out after {timeout:g} s")
    except OSError as exc:
        return CmdResult(126, "", f"{argv[0]}: {exc}")
    return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def atomic_write(path: Union[str, Path], data: Union[str, bytes], mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` via a temporary file + rename (readers never see half a file).

    The file is created with ``mode`` from the start, so a secret is never
    briefly world-readable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Union[str, Path], obj: Any, mode: int = 0o644) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode)


def read_json(path: Union[str, Path]) -> Optional[Any]:
    """Parsed JSON, or None if the file is missing, unreadable or invalid."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def file_age_s(path: Union[str, Path], now: Optional[float] = None) -> Optional[float]:
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime


def touch(path: Union[str, Path]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a"):
            pass
        os.utime(p, None)
    except OSError as exc:
        log.debug("cannot touch %s: %s", p, exc)


def read_text(path: Union[str, Path], limit: int = 65536) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


_SECRET_PATTERNS = [
    (re.compile(r"tskey-[A-Za-z0-9_-]+"), "tskey-<redacted>"),
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"), "<redacted-64hex>"),
    (re.compile(r"(?i)(\"?(?:node_key|key_hex|auth_key|api_key|password|psk|secret)\"?\s*[:=]\s*)(\"[^\"]*\"|\S+)"),
     r"\1<redacted>"),
]


def redact(text: str) -> str:
    """Remove keys and passwords from text for the diagnostics bundle."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


_SECRET_KEYS = {"node_key", "key_hex", "auth_key", "api_key", "password", "psk", "secret", "hash", "salt"}


def redact_obj(obj: Any) -> Any:
    """A copy of a JSON-like value with secret-named members replaced."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in _SECRET_KEYS and v is not None else redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj
