"""Shared fixtures: settings, a simulated sky, and throwaway Redis / PostGIS servers.

The Redis (port 6390) and PostgreSQL (port 5440) fixtures start private
server processes in temporary directories and skip the dependent tests when
the binaries are not installed. PostgreSQL refuses to run as root, so under
root the cluster is created and started as the ``postgres`` user.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from typing import Iterator, List, Optional

import pytest

from skynet_server.config import Settings

MASTER_KEY = b"test-master-key-0123456789abcdef0123456789"
REDIS_PORT = 6390
PG_PORT = 5440


def make_settings(**overrides) -> Settings:
    base = Settings(master_key=MASTER_KEY, admin_token="admin-secret", udp_port=None)
    return base.with_overrides(**overrides) if overrides else base


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# --- Redis -------------------------------------------------------------------------


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server is not installed")
    proc = None
    workdir = tempfile.mkdtemp(prefix="skynet-redis-")
    if not _port_open(REDIS_PORT):
        proc = subprocess.Popen([binary, "--port", str(REDIS_PORT), "--bind", "127.0.0.1", "--save", "",
                                 "--appendonly", "no", "--dir", workdir],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if _port_open(REDIS_PORT):
                break
            time.sleep(0.05)
        else:
            proc.kill()
            pytest.skip("redis-server did not start")
    yield f"redis://127.0.0.1:{REDIS_PORT}/0"
    if proc is not None:
        proc.terminate()
        proc.wait(5)
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture
def redis_prefix() -> str:
    return f"test-{uuid.uuid4().hex[:8]}:"


# --- PostgreSQL + PostGIS --------------------------------------------------------------


def _pg_bin(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    candidates = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}")) + sorted(
        glob.glob(f"/opt/homebrew/opt/postgresql*/bin/{name}"))
    return candidates[-1] if candidates else None


def _as_postgres(cmd: List[str]) -> List[str]:
    if os.geteuid() == 0:
        return ["runuser", "-u", "postgres", "--"] + cmd
    return cmd


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str]:
    initdb, pg_ctl = _pg_bin("initdb"), _pg_bin("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if os.geteuid() == 0 and shutil.which("runuser") is None:
        pytest.skip("running as root without runuser")
    base = tempfile.mkdtemp(prefix="skynet-pg-")
    data = os.path.join(base, "data")
    if os.geteuid() == 0:
        shutil.chown(base, "postgres", "postgres")
    started = False
    if not _port_open(PG_PORT):
        subprocess.run(_as_postgres([initdb, "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8"]),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opts = f"-p {PG_PORT} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off"
        subprocess.run(_as_postgres([pg_ctl, "-D", data, "-o", opts, "-l", os.path.join(base, "log"), "-w",
                                     "start"]), check=True, stdout=subprocess.DEVNULL)
        started = True
    yield f"postgresql://postgres@127.0.0.1:{PG_PORT}"
    if started:
        subprocess.run(_as_postgres([pg_ctl, "-D", data, "-m", "fast", "stop"]), stdout=subprocess.DEVNULL)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def database_url(pg_server: str) -> Iterator[str]:
    """A fresh, empty database (with PostGIS available) for one test."""
    asyncpg = pytest.importorskip("asyncpg")
    name = f"skynet_{uuid.uuid4().hex[:10]}"

    async def admin(sql: str) -> object:
        conn = await asyncpg.connect(f"{pg_server}/postgres")
        try:
            if sql.startswith("SELECT"):
                return await conn.fetchval(sql)
            await conn.execute(sql)
            return None
        finally:
            await conn.close()

    if not asyncio.run(admin("SELECT count(*) FROM pg_available_extensions WHERE name = 'postgis'")):
        pytest.skip("PostGIS is not installed for this PostgreSQL")
    asyncio.run(admin(f"CREATE DATABASE {name}"))
    yield f"{pg_server}/{name}"
    asyncio.run(admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
