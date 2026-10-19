import hashlib
import socket
import time

import numpy as np
import pytest

from fakeserver import HTTP_PORTS, FakeHttpServer, FakeUdpServer, bind_free, clip_request
from helpers import ATTITUDE, MASTER_KEY, T0_NS, small_config
from skynet_common.protocol import (ClipRequest, Heartbeat, Telemetry, decode_message, derive_node_key,
                                    verify_http_request)
from skynet_edge import messages, uplink as uplink_mod
from skynet_edge.clips import ClipManager
from skynet_edge.geometry import CameraModel
from skynet_edge.pipeline import Report
from skynet_edge.ringbuffer import FrameRing
from skynet_edge.sensors.gps import Fix
from skynet_edge.uplink import HttpsUplink, NodeAuth, NullUplink, UdpUplink, resolve_server_url
from skynet_edge.uploader import ClipUploader, complete_url_for

NODE = "node-test"
KEY = derive_node_key(MASTER_KEY, NODE)
FIX = Fix(33.6292, -112.3679, 357.2, 3, 0, "static")


def report(i=0):
    return Report("t-000001", T0_NS + i * 33_000_000, 142.5211 + i * 0.01, 65.1043, 12.4, 0.85, 0.91)


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_resolve_server_url():
    base = "http://mac-studio:8000"
    up = "http://127.0.0.1:8000/api/v1/clips/r1/upload-target"
    assert resolve_server_url(up, None, base, True) == "http://mac-studio:8000/api/v1/clips/r1/upload-target"
    assert resolve_server_url(up, None, base, False) == up
    assert resolve_server_url("http://0.0.0.0:8000/x", None, base, True) == "http://mac-studio:8000/x"
    assert resolve_server_url("http://[::1]:8000/x?a=1", None, base, True) == "http://mac-studio:8000/x?a=1"
    assert resolve_server_url("/blob/r1", "https://api.example.org/api/v1/clips/r1/upload-target", base,
                              True) == "https://api.example.org/blob/r1"
    presigned = "https://bucket.s3.amazonaws.com/c/r1.mp4?X-Amz-Signature=abc"
    assert resolve_server_url(presigned, up, base, True) == presigned
    assert resolve_server_url("http://localhost/x", None, None, True) == "http://localhost/x"  # nothing to use


def test_complete_url_keeps_prefix():
    assert complete_url_for("https://h/skynet/api/v1/clips/r1/upload-target", "r1") == \
        "https://h/skynet/api/v1/clips/r1/complete"
    assert complete_url_for("https://h/other", "r1") == "https://h/api/v1/clips/r1/complete"


def test_node_auth_headers_verify():
    h = NodeAuth(NODE, KEY, clock=lambda: T0_NS).headers("POST", "http://x:1/api/v1/clips/r/complete?q=1", b"{}")
    assert h["X-Skynet-Node"] == NODE and h["X-Skynet-Timestamp"] == str(T0_NS)
    assert verify_http_request(KEY, "POST", "/api/v1/clips/r/complete", T0_NS, b"{}", h["X-Skynet-Signature"])
    assert NodeAuth(NODE, None).headers("GET", "http://x/") == {}


def test_messages_are_signed_sequenced_and_small():
    sent = []
    up = NullUplink(NODE, KEY, sink=sent.append)
    assert up.send(messages.telemetry(NODE, FIX, ATTITUDE, report()))
    cam = CameraModel.from_fov(1920, 1080, 90.0)
    assert up.send(messages.heartbeat(NODE, T0_NS, FIX, ATTITUDE, cam, 59.8, {"fps": 59.8, "tracks_active": 2}))
    assert all(len(d) <= 1200 for d in sent)
    tel, hb = (decode_message(d, MASTER_KEY) for d in sent)
    assert isinstance(tel, Telemetry) and isinstance(hb, Heartbeat)
    assert hb.seq == tel.seq + 1 and tel.seq > T0_NS // 10**6 - 10**12  # ms-based start, monotonic across restarts
    assert up.encode({"type": "telemetry", "node_id": NODE, "pad": "x" * 2000}) is None
    assert up.stats["too_large"] == 1


def test_udp_round_trip_and_verified_clip_requests():
    server = FakeUdpServer(MASTER_KEY)
    cfg = small_config({"server": {"transport": "udp", "udp_host": "localhost", "udp_port": server.port}}).server
    got = []
    up = UdpUplink(cfg, NODE, KEY, on_command=got.append)
    up.start()
    try:
        assert up.server_addr is not None
        for i in range(3):
            assert up.send(messages.telemetry(NODE, FIX, ATTITUDE, report(i)))
        assert wait_for(lambda: len(server.messages) == 3)
        t = server.messages[0]
        assert isinstance(t, Telemetry) and t.node_id == NODE and t.timestamp_ns == T0_NS
        assert t.vector_target.azimuth_deg == pytest.approx(142.5211) and t.track_id == "t-000001"
        assert t.anomaly_score == pytest.approx(0.91) and t.gps_coordinates.altitude_m == pytest.approx(357.2)
        assert [m.seq for m in server.messages] == sorted(m.seq for m in server.messages)
        peer = server.peers[-1]
        url = "http://127.0.0.1:1/api/v1/clips/r1/upload-target"
        server.send(clip_request(MASTER_KEY, NODE, "r1", T0_NS, T0_NS + int(2e9), url), peer)
        server.send(clip_request(MASTER_KEY, NODE, "r1", T0_NS, T0_NS + int(2e9), url), peer)  # duplicate
        server.send(clip_request(MASTER_KEY, NODE, "r2", T0_NS, T0_NS + 1, url, sign_with=b"k" * 32), peer)
        server.send(clip_request(MASTER_KEY, "node-other", "r3", T0_NS, T0_NS + 1, url,
                                 sign_with=KEY), peer)  # signed with our key but addressed to another node
        server.send(b'{"type":"clip_request","request_id":"r4"}', peer)  # unsigned
        assert wait_for(lambda: up.stats["bad_commands"] == 3)
        assert [r.request_id for r in got] == ["r1"] and isinstance(got[0], ClipRequest)
    finally:
        up.stop()
        server.close()


def test_udp_host_is_re_resolved(monkeypatch):
    cfg = small_config({"server": {"transport": "udp", "udp_host": "mac-studio", "udp_port": 9999}}).server
    answers = [OSError("Name or service not known"), [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("100.64.0.7", 9999))],
               [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("100.64.0.9", 9999))]]

    def fake_getaddrinfo(host, port, type=0):
        assert host == "mac-studio"
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(uplink_mod.socket, "getaddrinfo", fake_getaddrinfo)
    up = UdpUplink(cfg, NODE, KEY)
    assert not up.resolve() and up.server_addr is None
    assert not up.send(messages.telemetry(NODE, FIX, ATTITUDE, report())) and up.stats["unresolved"] == 1
    assert up.resolve() and up.server_addr[0] in ("::ffff:100.64.0.7", "100.64.0.7")
    assert up.resolve() and up.server_addr[0].endswith("100.64.0.9")
    up.stop()


def _ring_with_frames(n, w=64, h=48, fps=30.0):
    ring = FrameRing(w, h, window_frames=120, hold_frames=30, prefault=False)
    for i in range(n):
        f = np.full((h * 3 // 2, w), 128, np.uint8)
        f[:h] = (i * 7) % 256
        ring.write(f, T0_NS + int(round(i * 1e9 / fps)))
    return ring


@pytest.mark.parametrize("style", ["relative", "absolute"])
def test_clip_upload_three_steps_with_retry(tmp_path, monkeypatch, style):
    http = FakeHttpServer(MASTER_KEY, put_url_style=style)
    http.fail_next_put = 1
    cfg = small_config({"server": {"https_base_url": http.base},
                        "clips": {"storage_dir": str(tmp_path), "record_events": False, "upload_max_attempts": 3}})
    ring = _ring_with_frames(60)
    clips = ClipManager(ring, cfg.clips, 30.0)
    clips.start()
    up = ClipUploader(NodeAuth(NODE, KEY), clips, cfg.server, cfg.clips, sleep=lambda s: None)
    up.start()
    try:
        # The server advertises its loopback address (no public URL configured); the node repairs it.
        req = ClipRequest("req-1", "ev-1", NODE, T0_NS + int(0.5e9), T0_NS + int(1.5e9),
                          "http://127.0.0.1:1/api/v1/clips/req-1/upload-target", time.time_ns())
        up.submit(req)
        assert up.drain(30)
    finally:
        up.stop()
        clips.stop()
        http.close()
    out = up.outcomes["req-1"]
    assert out.ok and out.attempts == 2, out.error
    body = http.uploads["req-1"]
    done = http.completes["req-1"]
    assert done["bytes"] == len(body) and done["sha256"] == hashlib.sha256(body).hexdigest()
    assert done["content_type"] == "video/mp4" and done["frames"] == 31
    assert done["t_start_ns"] == T0_NS + int(round(15 * 1e9 / 30)) and done["t_end_ns"] == T0_NS + int(round(45e9 / 30))
    assert body[4:8] == b"ftyp" and not http.auth_failures


def test_unavailable_clip_is_reported_not_uploaded(tmp_path):
    cfg = small_config({"clips": {"storage_dir": str(tmp_path), "record_events": False}})
    ring = _ring_with_frames(10)
    clips = ClipManager(ring, cfg.clips, 30.0)
    clips.start()
    up = ClipUploader(NodeAuth(NODE, KEY), clips, cfg.server, cfg.clips)
    up.start()
    up.submit(ClipRequest("old", "e", NODE, T0_NS - int(60e9), T0_NS - int(50e9), "http://x/u", time.time_ns()))
    assert up.drain(5)
    up.stop()
    clips.stop()
    assert not up.outcomes["old"].ok and "no longer buffered" in up.outcomes["old"].error


def test_https_uplink_batches_and_receives_commands():
    http = FakeHttpServer(MASTER_KEY)
    cfg = small_config({"server": {"transport": "https", "https_base_url": http.base, "batch_interval_s": 0.05,
                                   "command_poll_interval_s": 0.2}}).server
    got = []
    up = HttpsUplink(cfg, NODE, KEY, on_command=got.append)
    up.start()
    try:
        cmd = clip_request(MASTER_KEY, NODE, "r-https", T0_NS, T0_NS + 1, f"{http.base}/api/v1/clips/r/upload-target")
        http.pending_commands[NODE] = [cmd.decode()]
        for i in range(5):
            up.send(messages.telemetry(NODE, FIX, ATTITUDE, report(i)))
        assert wait_for(lambda: len(http.ingested) == 5)
        assert all(b.endswith(b"\n") for b in http.ingest_bodies)
        assert wait_for(lambda: [r.request_id for r in got] == ["r-https"])  # returned in the ingest response
        http.pending_commands[NODE] = [clip_request(MASTER_KEY, NODE, "r-poll", T0_NS, T0_NS + 1, "/u").decode(),
                                       clip_request(MASTER_KEY, NODE, "r-bad", T0_NS, T0_NS + 1, "/u",
                                                    sign_with=b"x" * 32).decode()]
        assert wait_for(lambda: len(got) == 2)  # idle: polled with node-auth
        assert got[1].request_id == "r-poll" and http.polls >= 1 and up.stats["bad_commands"] == 1
        assert not http.auth_failures
    finally:
        up.stop()
        http.close()


def test_https_uplink_retries_then_expires_old_messages():
    sock = socket.socket()
    port = bind_free(sock, HTTP_PORTS)
    sock.close()  # nothing listens here: connections are refused
    cfg = small_config({"server": {"transport": "https", "https_base_url": f"http://127.0.0.1:{port}",
                                   "batch_interval_s": 0.02, "max_message_age_s": 0.3,
                                   "request_timeout_s": 0.2}}).server
    up = HttpsUplink(cfg, NODE, KEY)
    up.start()
    up.send(messages.telemetry(NODE, FIX, ATTITUDE, report()))
    assert wait_for(lambda: up.stats["expired"] == 1, timeout=10)
    assert up.stats["errors"] >= 1 and up.pending() == 0
    up.stop(timeout=0.5)
