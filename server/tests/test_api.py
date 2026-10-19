"""HTTP API endpoints (docs/API.md) through FastAPI's TestClient."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient

from skynet_common.geodesy import look_angles
from skynet_common.protocol import decode_for_node, derive_node_key, sign_http_request
from skynet_server import sim
from skynet_server.app import create_app

from .conftest import MASTER_KEY, make_settings

TARGET = (33.55, -112.20, 9000.0)
ADMIN = {"Authorization": "Bearer admin-secret"}
ORIGIN = "https://skynet.example.org"


def zenith(node_id: str, lat: float, lon: float) -> sim.SimNode:
    return sim.SimNode(node_id, lat, lon, 300.0, yaw_deg=0.0, pitch_deg=90.0, fov_h_deg=90.0, fov_v_deg=90.0)


@pytest.fixture(params=["memory", "redis"])
def client(request, tmp_path, redis_prefix) -> Iterator[TestClient]:
    overrides = dict(storage_dir=str(tmp_path / "clips"), cors_origins=(ORIGIN,), public_url="http://mac-studio:8000")
    if request.param == "redis":
        overrides.update(state_backend="redis", redis_url=request.getfixturevalue("redis_url"),
                         redis_prefix=redis_prefix)
    with TestClient(create_app(make_settings(**overrides))) as c:
        yield c


def aimed(node_id: str, lat: float, lon: float) -> sim.SimNode:
    """A node whose camera points at TARGET (so the target is in its view)."""
    az, el, _ = look_angles(lat, lon, 300.0, *TARGET)
    return sim.SimNode(node_id, lat, lon, 300.0, yaw_deg=az, pitch_deg=el)


def network() -> sim.SimNetwork:
    return sim.SimNetwork(MASTER_KEY, [aimed("node-a", 33.40, -112.35), aimed("node-b", 33.72, -112.05)])


def ndjson(*lines: bytes) -> Dict:
    return {"content": b"\n".join(lines), "headers": {"Content-Type": "application/x-ndjson"}}


def sightings(net: sim.SimNetwork, t_ns: Optional[int] = None) -> List[bytes]:
    t = t_ns or time.time_ns()
    out = []
    for node in net.nodes:
        az, el, _ = look_angles(node.lat, node.lon, node.alt_m, *TARGET)
        out.append(net.sign(sim.telemetry_dict(node, t, az, el, "t1")))
    return out


def node_headers(node_id: str, method: str, path: str, body: bytes = b"", ts: Optional[int] = None) -> Dict[str, str]:
    ts = ts or time.time_ns()
    sig = sign_http_request(derive_node_key(MASTER_KEY, node_id), method, path, ts, body)
    return {"X-Skynet-Node": node_id, "X-Skynet-Timestamp": str(ts), "X-Skynet-Signature": sig}


def triangulate(client: TestClient) -> Dict:
    """Two nodes see the target; returns the ingest response."""
    net = network()
    hb = [net.sign(sim.heartbeat_dict(n, time.time_ns())) for n in net.nodes]
    resp = client.post("/api/v1/ingest", **ndjson(*hb, *sightings(net)))
    assert resp.status_code == 200
    return resp.json()


def test_health_and_stats(client: TestClient):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["mode"] == "standalone" and h["db"] == "disabled" and h["udp_port"] is None
    assert h["state"] == "ok" and h["state_backend"] in ("memory", "redis") and h["time_ns"] > 0
    triangulate(client)
    s = client.get("/api/v1/stats").json()
    assert s["packets_total"] == 4 and s["matches_total"] == 1 and s["active_nodes"] == 2 and s["active_tracks"] == 1
    assert set(s["processing_latency_ms"]) == {"p50", "p95", "p99", "max"}
    text = client.get("/metrics").text
    assert "skynet_packets_total 4" in text and 'skynet_processing_latency_ms{quantile="0.99"}' in text
    assert client.get("/api/v1/metrics").status_code == 200


def test_node_registration_requires_admin(client: TestClient):
    assert client.post("/api/v1/nodes", json={}).status_code == 401
    assert client.post("/api/v1/nodes", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    resp = client.post("/api/v1/nodes", json={"label": "Roof", "node_id": "roof-1"}, headers=ADMIN)
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"node_id": "roof-1", "node_key": derive_node_key(MASTER_KEY, "roof-1").hex(),
                    "ingest_url": "http://mac-studio:8000/api/v1/ingest", "udp_endpoint": None}
    generated = client.post("/api/v1/nodes", json={}, headers=ADMIN).json()
    assert len(generated["node_id"]) == 36
    assert client.post("/api/v1/nodes", json={"node_id": "bad id"}, headers=ADMIN).status_code == 422
    nodes = {n["node_id"]: n for n in client.get("/api/v1/nodes").json()["nodes"]}
    assert nodes["roof-1"]["label"] == "Roof" and nodes["roof-1"]["online"] is False


def test_admin_api_refused_through_tailscale_funnel(client: TestClient):
    # tailscaled adds this header to every request that came in from the public internet.
    funnel = {**ADMIN, "Tailscale-Funnel-Request": "?1"}
    assert client.post("/api/v1/nodes", json={}, headers=funnel).status_code == 403
    assert client.get("/api/v1/volunteers", headers=funnel).status_code == 403
    # Public endpoints are unaffected.
    assert client.get("/api/v1/health", headers={"Tailscale-Funnel-Request": "?1"}).status_code == 200


def test_ingest_rejections_rounding_and_commands_once(client: TestClient):
    net = network()
    good = sightings(net)
    resp = client.post("/api/v1/ingest", **ndjson(good[0], b"", b"{bad", good[0]))
    assert resp.json() == {"accepted": 1, "rejected": [{"line": 3, "reason": "bad_json"},
                                                      {"line": 4, "reason": "replay"}], "commands": []}
    out = triangulate(client)
    assert out["accepted"] == 4
    cmds = [json.loads(c) for c in out["commands"]]
    assert sorted(c["node_id"] for c in cmds) == ["node-a", "node-b"]
    for raw in out["commands"]:
        node_id = json.loads(raw)["node_id"]
        assert decode_for_node(raw.encode(), net.key(node_id)).type == "clip_request"
    # Handed out once.
    again = client.post("/api/v1/ingest", **ndjson(net.sign(sim.heartbeat_dict(net.nodes[0], time.time_ns()))))
    assert again.json()["commands"] == []
    nodes = {n["node_id"]: n for n in client.get("/api/v1/nodes", params={"online_only": True}).json()["nodes"]}
    assert nodes["node-a"]["lat"] == 33.4 and nodes["node-a"]["lon"] == -112.35 and nodes["node-a"]["online"]
    too_many = b"\n".join([b"{}"] * 2001)
    assert client.post("/api/v1/ingest", content=too_many).status_code == 413


def test_commands_endpoint_node_auth(client: TestClient):
    net = network()
    hb = net.sign(sim.heartbeat_dict(net.nodes[0], time.time_ns()))
    # Only node-a's telemetry arrives over HTTPS with no other node in the request.
    client.post("/api/v1/ingest", **ndjson(hb, sightings(net)[0]))
    client.post("/api/v1/ingest", **ndjson(sightings(net)[1]))  # node-b's commands go to node-b here
    path = "/api/v1/nodes/node-a/commands"
    assert client.get(path).status_code == 401
    bad = node_headers("node-a", "GET", path)
    bad["X-Skynet-Signature"] = "0" * 64
    assert client.get(path, headers=bad).status_code == 401
    stale = node_headers("node-a", "GET", path, ts=time.time_ns() - 120_000_000_000)
    assert client.get(path, headers=stale).status_code == 401
    # A validly signed request from another node may not read node-a's queue.
    assert client.get(path, headers=node_headers("node-b", "GET", path)).status_code == 403
    headers = node_headers("node-a", "GET", path)
    first = client.get(path, headers=headers)
    assert first.status_code == 200 and len(first.json()["commands"]) == 1
    assert client.get(path, headers=headers).status_code == 401  # replayed request
    assert client.get(path, headers=node_headers("node-a", "GET", path)).json() == {"commands": []}


def test_events_cursor_paging(client: TestClient):
    triangulate(client)
    events = client.get("/api/v1/events", params={"limit": 1000}).json()
    types = [e["type"] for e in events["events"]]
    assert {"node", "triangulation", "track", "clip"} <= set(types)
    ids = [e["id"] for e in events["events"]]
    assert events["cursor"] == ids[-1]
    tri = next(e for e in events["events"] if e["type"] == "triangulation")["data"]
    assert {r["node_lat"] for r in tri["rays"]} == {33.4, 33.72}  # rounded like public node positions
    page = client.get("/api/v1/events", params={"after": ids[0], "limit": 2}).json()
    assert [e["id"] for e in page["events"]] == ids[1:3] and page["cursor"] == ids[2]
    empty = client.get("/api/v1/events", params={"after": ids[-1]}).json()
    assert empty == {"events": [], "cursor": ids[-1]}
    latest = client.get("/api/v1/events", params={"limit": 2}).json()
    assert [e["id"] for e in latest["events"]] == ids[-2:]
    assert client.get("/api/v1/events", params={"after": "nope"}).status_code == 400
    assert client.get("/api/v1/events", params={"limit": 5000}).status_code == 422


def test_tracks(client: TestClient):
    triangulate(client)
    tracks = client.get("/api/v1/tracks").json()["tracks"]
    assert len(tracks) == 1
    t = tracks[0]
    assert sorted(t["node_ids"]) == ["node-a", "node-b"]
    assert t["point_count"] == 1 and len(t["points"]) == 1 and t["classification"] == "unknown"
    assert abs(t["last_point"]["alt_m"] - TARGET[2]) < 50 and t["last_point"]["sigma_m"] > 0
    detail = client.get(f"/api/v1/tracks/{t['track_id']}").json()
    assert len(detail["points"]) == 1 and len(detail["clips"]) == 2
    assert {c["status"] for c in detail["clips"]} == {"requested"} and detail["clips"][0]["download_url"] is None
    assert client.get("/api/v1/tracks/T-missing").status_code == 404
    assert client.get("/api/v1/tracks", params={"classification": "hypersonic"}).json() == {"tracks": []}
    assert client.get("/api/v1/tracks", params={"classification": "ufo"}).status_code == 422
    assert client.get("/api/v1/tracks", params={"since_ns": time.time_ns() + 10**12}).json() == {"tracks": []}


def test_clip_upload_flow_with_local_storage(client: TestClient):
    out = triangulate(client)
    req = next(json.loads(c) for c in out["commands"] if json.loads(c)["node_id"] == "node-a")
    rid = req["request_id"]
    assert req["upload_url"] == f"http://mac-studio:8000/api/v1/clips/{rid}/upload-target"
    target_path = f"/api/v1/clips/{rid}/upload-target"
    assert client.post(target_path).status_code == 401
    assert client.post(target_path, headers=node_headers("node-b", "POST", target_path)).status_code == 403
    target = client.post(target_path, headers=node_headers("node-a", "POST", target_path)).json()
    assert target == {"method": "PUT", "url": f"http://mac-studio:8000/api/v1/clips/{rid}/data",
                      "headers": {"Content-Type": "video/mp4"}}
    data_path = f"/api/v1/clips/{rid}/data"
    video = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40
    # The PUT is node-authenticated over its body hash.
    assert client.put(data_path, content=video).status_code == 401
    forged = node_headers("node-a", "PUT", data_path, b"something else")
    assert client.put(data_path, content=video, headers=forged).status_code == 401
    assert client.put(data_path, content=video, headers=node_headers("node-b", "PUT", data_path, video)).status_code == 403
    put = client.put(data_path, content=video, headers={**node_headers("node-a", "PUT", data_path, video),
                                                         "Content-Type": "video/mp4"})
    assert put.status_code == 200 and put.json() == {"bytes": len(video), "sha256": hashlib.sha256(video).hexdigest()}
    complete_path = f"/api/v1/clips/{rid}/complete"
    wrong = json.dumps({"bytes": len(video), "sha256": "0" * 64}).encode()
    assert client.post(complete_path, content=wrong,
                       headers=node_headers("node-a", "POST", complete_path, wrong)).status_code == 409
    body = json.dumps({"bytes": len(video), "sha256": hashlib.sha256(video).hexdigest(), "content_type": "video/mp4",
                       "t_start_ns": req["t_start_ns"], "t_end_ns": req["t_end_ns"], "frames": 600}).encode()
    done = client.post(complete_path, content=body, headers=node_headers("node-a", "POST", complete_path, body))
    assert done.status_code == 200 and done.json() == {"status": "uploaded"}
    meta = client.get(f"/api/v1/clips/{rid}").json()
    assert meta["status"] == "uploaded" and meta["bytes"] == len(video)
    assert meta["download_url"] == f"http://mac-studio:8000/api/v1/clips/{rid}/download"
    assert client.get(f"/api/v1/clips/{rid}/download").content == video
    clips = client.get(f"/api/v1/tracks/{meta['track_id']}").json()["clips"]
    assert {c["status"] for c in clips} == {"uploaded", "requested"}
    events = client.get("/api/v1/events", params={"limit": 1000}).json()["events"]
    assert any(e["type"] == "clip" and e["data"]["status"] == "uploaded" for e in events)
    assert client.get("/api/v1/clips/unknown").status_code == 404


def test_coverage(client: TestClient):
    # Two zenith cameras 4 km apart east-west at 33.5 N.
    net = sim.SimNetwork(MASTER_KEY, [zenith("z-west", 33.5, -112.0215), zenith("z-east", 33.5, -111.9785)])
    client.post("/api/v1/ingest", **ndjson(*(net.sign(sim.heartbeat_dict(n, time.time_ns())) for n in net.nodes)))
    params = {"min_lat": 33.49, "min_lon": -112.03, "max_lat": 33.51, "max_lon": -111.97, "cell_km": 1.0}
    cov = client.get("/api/v1/coverage", params=params).json()
    assert cov["type"] == "FeatureCollection" and cov["params"]["cell_km"] == 1.0
    rows, cols = cov["params"]["rows"], cov["params"]["cols"]
    assert len(cov["features"]) == rows * cols
    assert sum(cov["summary"].values()) == rows * cols
    by_cell = {}
    for f in cov["features"]:
        ring = f["geometry"]["coordinates"][0]
        assert f["geometry"]["type"] == "Polygon" and ring[0] == ring[-1] and len(ring) == 5
        lon = (ring[0][0] + ring[1][0]) / 2
        by_cell[round(lon, 3)] = f["properties"]
    middle = min(by_cell, key=lambda lon: abs(lon + 112.0))
    # 3 km above the midpoint each camera sees the point 34 degrees off its axis: stereo, 67 degree parallax.
    assert by_cell[middle] == {"seeing": 2, "stereo_pairs": 1, "status": "stereo"}
    # Above the western camera the eastern one would need 53 degrees off axis (> 45): mono.
    west = min(by_cell, key=lambda lon: abs(lon + 112.0215))
    assert by_cell[west]["status"] == "mono"
    assert isinstance(cov["suggestions"], list)
    far = client.get("/api/v1/coverage", params={**params, "min_lat": 35.0, "max_lat": 35.05}).json()
    assert far["summary"]["blind"] == len(far["features"]) and far["suggestions"] == []
    too_big = client.get("/api/v1/coverage", params={"min_lat": 0, "min_lon": 0, "max_lat": 50, "max_lon": 50,
                                                     "cell_km": 1})
    assert too_big.status_code == 400
    assert client.get("/api/v1/coverage").status_code == 422


def test_cors_preflight(client: TestClient):
    ok = client.options("/api/v1/tracks", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGIN
    post = client.options("/api/v1/volunteers", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                                         "Access-Control-Request-Headers": "content-type"})
    assert post.status_code == 200 and "POST" in post.headers["access-control-allow-methods"]
    evil = client.options("/api/v1/tracks", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers
    simple = client.get("/api/v1/health", headers={"Origin": ORIGIN})
    assert simple.headers["access-control-allow-origin"] == ORIGIN


def test_volunteer_endpoints_without_database(client: TestClient):
    body = {"email": "a@b.co", "country": "US", "consent": True}
    assert client.post("/api/v1/volunteers", json=body).json() == {"detail": "sign-up unavailable"}
    assert client.post("/api/v1/volunteers", json=body).status_code == 503
    assert client.get("/api/v1/volunteers/summary").status_code == 503


def test_live_websocket(client: TestClient):
    with client.websocket_connect("/api/v1/live") as ws:
        client.post("/api/v1/nodes", json={"node_id": "ws-node"}, headers=ADMIN)
        event = ws.receive_json()
        assert event["type"] == "node" and event["data"]["node_id"] == "ws-node" and "-" in event["id"]


def test_coverage_uses_rounded_node_positions():
    from skynet_server.coverage import camera_nodes
    from skynet_server.models import NodeRecord

    node = NodeRecord("n", lat=33.40371, lon=-112.35189, alt_m=357.2, yaw_deg=0.0, pitch_deg=90.0, roll_deg=0.0,
                      fov_h_deg=90.0, fov_v_deg=90.0, last_seen_ns=time.time_ns())
    (cam,) = camera_nodes([node], time.time_ns(), 60_000_000_000, decimals=2)
    assert (cam.lat, cam.lon, cam.alt_m) == (33.4, -112.35, 360.0)
