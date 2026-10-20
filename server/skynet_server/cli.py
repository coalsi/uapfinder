"""``skynet-server`` command line: serve, migrate, keygen, node-key, enroll-codes.

Configuration comes from ``SKYNET_*`` environment variables (see
.env.example); the few flags here override the most common ones for local
runs. ``serve`` runs uvicorn with the API and, on the same event loop, the
UDP listener; uvloop is used when installed (uvicorn ``loop="auto"``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, Settings


def _load_env_file(path: Optional[str]) -> None:
    """Minimal KEY=VALUE loader; existing environment variables win."""
    if not path:
        return
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"env file {path} not found")
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]  # inline comment
        os.environ.setdefault(key.strip(), value)


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from None


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    # Flags become environment variables so that worker processes, which
    # build their own Settings, see them too.
    for flag, name in (("host", "SKYNET_HTTP_HOST"), ("port", "SKYNET_HTTP_PORT"), ("udp_port", "SKYNET_UDP_PORT"),
                       ("workers", "SKYNET_WORKERS")):
        value = getattr(args, flag, None)
        if value is not None:
            os.environ[name] = str(value)
    settings = _settings()
    if settings.mode != "standalone":
        raise SystemExit("`serve` runs the standalone server; unset SKYNET_MODE=vercel")
    if settings.master_key is None and not settings.allow_unsigned:
        logging.warning("SKYNET_MASTER_KEY is not set: every signed message will be rejected (no_key)")
    common = dict(
        host=settings.http_host, port=settings.http_port, loop="auto", log_level=settings.log_level,
        # Trust X-Forwarded-* from a local reverse proxy (Tailscale Funnel/Serve)
        # by default, from anywhere when SKYNET_TRUST_PROXY=true.
        proxy_headers=True, forwarded_allow_ips="*" if settings.trust_proxy else "127.0.0.1",
        access_log=args.access_log, ws_max_size=1 << 20,
    )
    if settings.workers > 1:
        if sys.platform == "darwin":
            logging.warning("macOS does not spread UDP datagrams over SO_REUSEPORT sockets; "
                            "all UDP traffic will reach one worker")
        uvicorn.run("skynet_server.app:create_app", factory=True, workers=settings.workers, **common)
    else:
        uvicorn.Server(uvicorn.Config(create_app(settings), **common)).run()
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from .persistence import MIGRATIONS_DIR, migrate

    settings = _settings()
    url = args.database_url or settings.database_url_unpooled or settings.database_url
    if not url:
        raise SystemExit("set SKYNET_DATABASE_URL (or DATABASE_URL / POSTGRES_URL) or pass --database-url")
    applied = asyncio.run(migrate(url, Path(args.dir) if args.dir else MIGRATIONS_DIR))
    print("applied: " + ", ".join(applied) if applied else "database schema is up to date")
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(48))
    return 0


def cmd_node_key(args: argparse.Namespace) -> int:
    from skynet_common.protocol import derive_node_key

    master = os.environ.get("SKYNET_MASTER_KEY", "")
    if not master:
        raise SystemExit("SKYNET_MASTER_KEY is not set")
    print(derive_node_key(master.encode(), args.node_id).hex())
    return 0


async def _codes_via_db(url: str, count: int, label: Optional[str], hours: float) -> List[dict]:
    import time

    from . import enrollment as enr
    from .persistence import Database

    db = Database(url, statement_cache=0, max_size=1)
    await db.start()
    try:
        now = time.time_ns()
        expires = now + int(hours * 3600e9)
        made: List[str] = []
        for _ in range(10):
            if len(made) >= count:
                break
            made += await db.insert_enrollment_codes([enr.new_code() for _ in range(count - len(made))], label, now,
                                                     expires)
    finally:
        await db.close()
    return [{"code": enr.format_code(c), "expires_ns": expires} for c in made]


def _codes_via_api(server: str, token: str, count: int, label: Optional[str], hours: float) -> List[dict]:
    import json
    import urllib.request

    body = json.dumps({"count": count, "label": label, "expires_in_h": hours}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/api/v1/enrollments", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - URL given by the coordinator
        return json.loads(resp.read())["codes"]


def cmd_enroll_codes(args: argparse.Namespace) -> int:
    import datetime

    if not 1 <= args.count <= 200:
        raise SystemExit("--count must be 1-200")
    if args.server:
        token = os.environ.get("SKYNET_ADMIN_TOKEN", "")
        if not token:
            raise SystemExit("set SKYNET_ADMIN_TOKEN to create codes through the API")
        codes = _codes_via_api(args.server, token, args.count, args.label, args.expires_h)
    else:
        settings = _settings()
        url = args.database_url or settings.database_url_unpooled or settings.database_url
        if not url:
            raise SystemExit("set SKYNET_DATABASE_URL (or pass --database-url, or --server URL to use the API)")
        codes = asyncio.run(_codes_via_db(url, args.count, args.label, args.expires_h))
    for c in codes:
        exp = datetime.datetime.fromtimestamp(c["expires_ns"] / 1e9, datetime.timezone.utc)
        print(f"{c['code']}  expires {exp:%Y-%m-%d %H:%M} UTC")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="skynet-server", description="SKY-NET server")
    parser.add_argument("--env-file", help="load KEY=VALUE settings from this file first")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the standalone HTTP API and UDP listener")
    serve.add_argument("--host", help="HTTP bind address (SKYNET_HTTP_HOST)")
    serve.add_argument("--port", type=int, help="HTTP port (SKYNET_HTTP_PORT)")
    serve.add_argument("--udp-port", type=int, help="UDP port, 0 to disable (SKYNET_UDP_PORT)")
    serve.add_argument("--workers", type=int, help="worker processes, redis backend only (SKYNET_WORKERS)")
    serve.add_argument("--access-log", action="store_true", help="log every HTTP request")
    serve.set_defaults(func=cmd_serve)

    mig = sub.add_parser("migrate", help="apply pending database migrations (idempotent)")
    mig.add_argument("--database-url", help="override SKYNET_DATABASE_URL (use Neon's direct, unpooled URL)")
    mig.add_argument("--dir", help="migrations directory (default: server/migrations)")
    mig.set_defaults(func=cmd_migrate)

    key = sub.add_parser("keygen", help="print a new random master key")
    key.set_defaults(func=cmd_keygen)

    nk = sub.add_parser("node-key", help="print the HMAC key of a node (derived from SKYNET_MASTER_KEY)")
    nk.add_argument("node_id")
    nk.set_defaults(func=cmd_node_key)

    ec = sub.add_parser("enroll-codes", help="create single-use claim codes for new nodes")
    ec.add_argument("--count", type=int, default=1, help="how many codes (1-200)")
    ec.add_argument("--label", help="free text stored with the codes, e.g. 'Phoenix pilot'")
    ec.add_argument("--expires-h", type=float, default=720.0, help="validity in hours (default 30 days)")
    ec.add_argument("--database-url", help="write to this database (default SKYNET_DATABASE_URL)")
    ec.add_argument("--server", help="instead, call POST /api/v1/enrollments on this server (SKYNET_ADMIN_TOKEN)")
    ec.set_defaults(func=cmd_enroll_codes)

    args = parser.parse_args(argv)
    _load_env_file(args.env_file)
    logging.basicConfig(level=os.environ.get("SKYNET_LOG_LEVEL", "info").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
