"""The Vercel entrypoint (api/index.py), its requirements and vercel.json."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from skynet_server import sim

from .conftest import MASTER_KEY

ROOT = Path(__file__).resolve().parents[2]
HTTP_PORT = 8101


def _names(requirements):
    return {re.split(r"[\[<>=~! ;]", r.strip(), maxsplit=1)[0].lower().replace("_", "-")
            for r in requirements if r.strip() and not r.strip().startswith("#")}


def test_requirements_cover_runtime_dependencies():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+

    needed = set()
    for pkg in ("server", "common"):
        deps = tomllib.loads((ROOT / pkg / "pyproject.toml").read_text())["project"]["dependencies"]
        needed |= _names(deps)
    needed -= {"skynet-common", "uvicorn"}  # imported from source / provided by the Vercel runtime
    have = _names((ROOT / "requirements.txt").read_text().splitlines())
    assert needed <= have, needed - have


def test_vercel_json_routes_api_to_the_function():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    fn = cfg["functions"]["api/index.py"]
    assert fn["maxDuration"] <= 300 and len(fn["excludeFiles"]) <= 256  # Hobby limit; schema maxLength
    assert {"source": "/api/(.*)", "destination": "/api/index"} in cfg["rewrites"]
    assert (ROOT / ".python-version").read_text().strip() in ("3.12", "3.13", "3.14")


def _serve(env: dict) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.index:app", "--port", str(HTTP_PORT),
                             "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/v1/health", timeout=0.5)
            break
        except urllib.error.HTTPError:
            break
        except OSError:
            time.sleep(0.1)
    return proc


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SKYNET_", "REDIS", "KV_", "DATABASE"))}
    # Import from source, as on Vercel: no installed skynet packages on the path.
    env.pop("PYTHONPATH", None)
    env.update(VERCEL="1", SKYNET_MASTER_KEY=MASTER_KEY.decode(), SKYNET_ADMIN_TOKEN="admin-secret", **extra)
    return env


def test_serves_the_api_in_vercel_mode(redis_url, redis_prefix):
    proc = _serve(_env(REDIS_URL=redis_url, SKYNET_REDIS_PREFIX=redis_prefix,
                       VERCEL_PROJECT_PRODUCTION_URL="skynet.example.vercel.app"))
    try:
        health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/v1/health"))
        assert health["mode"] == "vercel" and health["state_backend"] == "redis" and health["udp_port"] is None
        network = sim.SimNetwork(MASTER_KEY, [sim.SimNode("node-a", 33.4, -112.35, 300.0)])
        body = network.sign(sim.heartbeat_dict(network.nodes[0], time.time_ns()))
        req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/api/v1/ingest", data=body, method="POST",
                                     headers={"Content-Type": "application/x-ndjson"})
        assert json.load(urllib.request.urlopen(req))["accepted"] == 1
        reg = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/api/v1/nodes", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer admin-secret"})
        created = json.load(urllib.request.urlopen(reg))
        assert created["ingest_url"] == "https://skynet.example.vercel.app/api/v1/ingest"
        assert created["udp_endpoint"] is None
        stats = json.load(urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/v1/stats"))
        assert stats["packets_total"] == 1 and stats["active_nodes"] >= 1
    finally:
        proc.terminate()
        proc.wait(10)


def test_misconfiguration_is_reported_per_request():
    proc = _serve(_env())  # vercel mode without any Redis URL
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/v1/health")
        assert err.value.code == 500
        assert "SKYNET_REDIS_URL" in json.load(err.value)["detail"]
    finally:
        proc.terminate()
        proc.wait(10)
