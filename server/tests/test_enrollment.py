"""Claim-code enrollment: POST /api/v1/enrollments (admin) and POST /api/v1/enroll (public)."""

from __future__ import annotations

import asyncio
import threading
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from skynet_common.protocol import derive_node_key
from skynet_server import enrollment as enr
from skynet_server.app import create_app
from skynet_server.config import ConfigError, Settings
from skynet_server.persistence import migrate

from .conftest import MASTER_KEY, make_settings

ADMIN = {"Authorization": "Bearer admin-secret"}
HW = {"serial": "1421224012345", "model": "Jetson Orin Nano", "camera": "imx477", "gps": "pps",
      "software_version": "0.2.0", "unexpected": "ignored"}


# --- without a database ---------------------------------------------------------------


def test_codes_and_normalisation():
    for _ in range(200):
        c = enr.new_code()
        assert len(c) == 8 and set(c) <= set(enr.CROCKFORD) and not set(c) & set("ILOU")
    assert enr.normalize_code("7kq2-m9xd") == "7KQ2M9XD" == enr.normalize_code(" 7KQ2 M9XD ")
    assert enr.normalize_code("oil0-1lio") == "01101110"
    for bad in ("7KQ2-M9X", "7KQ2-M9XU", "7KQ2M9XD9", None, 1234, "x" * 40, "7KQ2;M9XD"):
        assert enr.normalize_code(bad) is None
    assert enr.format_code("7KQ2M9XD") == "7KQ2-M9XD"


def test_no_database_means_503():
    with TestClient(create_app(make_settings())) as c:
        assert c.post("/api/v1/enroll", json={"code": "7KQ2-M9XD"}).status_code == 503
        assert c.post("/api/v1/enrollments", json={"count": 1}, headers=ADMIN).status_code == 503


def test_settings_from_env():
    s = Settings.from_env({"SKYNET_TAILSCALE_AUTHKEY": "tskey-auth-abc", "SKYNET_NODE_TRANSPORT": "HTTPS",
                           "SKYNET_NODE_UDP_HOST": "mac-studio", "SKYNET_NODE_UDP_PORT": "9998",
                           "SKYNET_TAILSCALE_TAGS": "tag:a, tag:b"})
    assert (s.tailscale_authkey, s.node_transport, s.node_udp_host, s.node_udp_port) == (
        "tskey-auth-abc", "https", "mac-studio", 9998)
    assert s.tailscale_tags == ("tag:a", "tag:b") and s.enroll_rate_per_hour == 10
    d = Settings.from_env({})
    assert d.tailscale_authkey is None and d.node_transport is None and d.tailscale_tags == ("tag:skynet-node",)
    for bad in ({"SKYNET_NODE_TRANSPORT": "tcp"}, {"SKYNET_NODE_UDP_PORT": "70000"},
                {"SKYNET_TAILSCALE_TAGS": "skynet"}):
        with pytest.raises(ConfigError):
            Settings.from_env(bad)


def test_connection_info_defaults():
    s = make_settings(public_url="https://api.uapfinder.example", udp_port=9999)
    info = enr.connection_info(s, "10.0.0.2", "http://10.0.0.2:8000")
    assert info == {"transport": "udp", "udp_host": "api.uapfinder.example", "udp_port": 9999,
                    "https_base_url": "https://api.uapfinder.example"}
    s = make_settings(udp_public_endpoint="udp.example.org:7777", udp_port=9999)
    assert enr.connection_info(s, "h", "http://h:8000")[
        "udp_host"] == "udp.example.org" and enr.connection_info(s, "h", "http://h:8000")["udp_port"] == 7777
    s = make_settings(mode="vercel", udp_port=None)
    info = enr.connection_info(s, "skynet.vercel.app", "https://skynet.vercel.app/")
    assert info["transport"] == "https" and info["https_base_url"] == "https://skynet.vercel.app"
    s = make_settings(node_transport="https", node_udp_host="mac-studio", node_udp_port=9000)
    assert enr.connection_info(s, "x", "http://x") == {"transport": "https", "udp_host": "mac-studio",
                                                       "udp_port": 9000, "https_base_url": "http://x"}


# --- with PostGIS -----------------------------------------------------------------------


@pytest.fixture
def db_url(database_url) -> str:
    asyncio.run(migrate(database_url))
    return database_url


@pytest.fixture
def client(db_url) -> Iterator[TestClient]:
    settings = make_settings(database_url=db_url, enroll_rate_per_hour=1000, tailscale_authkey="tskey-auth-k123-xyz",
                             node_udp_host="mac-studio", public_url="http://mac-studio:8000")
    with TestClient(create_app(settings)) as c:
        yield c


def make_codes(c: TestClient, **body) -> list:
    resp = c.post("/api/v1/enrollments", json=body or {"count": 2, "label": "Phoenix pilot"}, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()["codes"]


def test_admin_only_and_not_through_funnel(client: TestClient):
    assert client.post("/api/v1/enrollments", json={"count": 1}).status_code == 401
    assert client.post("/api/v1/enrollments", json={"count": 1},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/enrollments", json={"count": 1},
                       headers={**ADMIN, "Tailscale-Funnel-Request": "?1"}).status_code == 403
    assert client.get("/api/v1/enrollments").status_code == 401
    for bad in ({"count": 0}, {"count": 201}, {"expires_in_h": 0}, {"label": "x" * 201}, {"cnt": 1}):
        assert client.post("/api/v1/enrollments", json=bad, headers=ADMIN).status_code == 422


def test_enroll_flow(client: TestClient):
    codes = make_codes(client, count=3, label="Phoenix pilot", expires_in_h=720)
    assert len(codes) == 3 and len({c["code"] for c in codes}) == 3
    code = codes[0]["code"]
    assert len(code) == 9 and code[4] == "-"
    resp = client.post("/api/v1/enroll", json={"code": code.lower().replace("-", ""), "hardware": HW})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["node_key"] == derive_node_key(MASTER_KEY, body["node_id"]).hex()
    assert body["transport"] == "https"  # this test server runs without the UDP listener
    assert body["udp_host"] == "mac-studio" and body["udp_port"] == 9999
    assert body["https_base_url"] == "http://mac-studio:8000"
    assert body["tailscale"] == {"auth_key": "tskey-auth-k123-xyz", "tags": ["tag:skynet-node"]}
    # Single use.
    again = client.post("/api/v1/enroll", json={"code": code})
    assert again.status_code == 410
    assert client.post("/api/v1/enroll", json={"code": "ZZZZ-ZZZZ"}).status_code == 404
    assert client.post("/api/v1/enroll", json={"code": "not a code"}).status_code == 404
    assert client.post("/api/v1/enroll", json={}).status_code == 422
    # The new node is registered, labelled, and the code records it.
    nodes = {n["node_id"]: n for n in client.get("/api/v1/nodes").json()["nodes"]}
    assert body["node_id"] in nodes
    rows = {r["code"]: r for r in client.get("/api/v1/enrollments", headers=ADMIN).json()["codes"]}
    used = rows[code]
    assert used["node_id"] == body["node_id"] and used["used_ns"] and used["label"] == "Phoenix pilot"
    assert used["hardware"]["camera"] == "imx477" and "unexpected" not in used["hardware"]
    assert rows[codes[1]["code"]]["node_id"] is None


def test_expired_code(client: TestClient):
    code = make_codes(client, count=1, expires_in_h=0.0000001)[0]["code"]
    import time

    time.sleep(0.01)
    assert client.post("/api/v1/enroll", json={"code": code}).status_code == 410


def test_concurrent_claims_produce_one_node(client: TestClient):
    code = make_codes(client, count=1)[0]["code"]
    results = []

    def go():
        results.append(client.post("/api/v1/enroll", json={"code": code}).status_code)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == [201] + [410] * 5


def test_rate_limit(db_url):
    settings = make_settings(database_url=db_url, enroll_rate_per_hour=3)
    with TestClient(create_app(settings)) as c:
        codes = [c.post("/api/v1/enroll", json={"code": "ZZZZ-ZZZZ"}).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]


def test_no_tailscale_without_key(db_url):
    with TestClient(create_app(make_settings(database_url=db_url))) as c:
        code = make_codes(c, count=1)[0]["code"]
        body = c.post("/api/v1/enroll", json={"code": code}).json()
    assert body["tailscale"] is None and body["https_base_url"].startswith("http://testserver")


def test_cli_enroll_codes(db_url, capsys, monkeypatch):
    from skynet_server.cli import main

    monkeypatch.setenv("SKYNET_DATABASE_URL", db_url)
    assert main(["enroll-codes", "--count", "3", "--label", "cli"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3 and all(enr.normalize_code(line.split()[0]) for line in lines)
