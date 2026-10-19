import hashlib
import json

import pytest

from skynet_common import protocol as p

MASTER = b"test-master-key-0123456789abcdef"

BLUEPRINT_PACKET = {
    "node_id": "4f9a2c1e-7b3d-4e8a-9c6f-1a2b3c4d5e6f",
    "gps_coordinates": {"latitude": 33.6292, "longitude": -112.3679, "altitude_m": 357.2},
    "imu_orientation": {"pitch_deg": 45.00, "roll_deg": 0.00, "yaw_deg": 0.00},
    "timestamp_ns": 1782345600000000000,
    "vector_target": {"azimuth_deg": 142.521, "elevation_deg": 65.104},
    "pixel_metrics": {"angular_velocity_deg_s": 12.4, "relative_intensity": 0.85},
}


def node_key(node_id=BLUEPRINT_PACKET["node_id"]):
    return p.derive_node_key(MASTER, node_id)


def test_blueprint_packet_signed_round_trip():
    data = p.encode_signed(BLUEPRINT_PACKET, node_key())
    assert data.endswith(b'"}') and b',"sig":"' in data
    assert len(data) < p.MAX_DATAGRAM_BYTES
    msg = p.decode_message(data, MASTER)
    assert isinstance(msg, p.Telemetry)
    assert msg.vector_target.azimuth_deg == pytest.approx(142.521)
    assert msg.timestamp_ns == 1782345600000000000
    # Still valid JSON that a plain parser can read.
    assert json.loads(data)["node_id"] == BLUEPRINT_PACKET["node_id"]


def test_tampered_packet_rejected():
    data = p.encode_signed(BLUEPRINT_PACKET, node_key())
    tampered = data.replace(b"142.521", b"142.522")
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(tampered, MASTER)
    assert exc.value.reason == "bad_signature"


def test_signature_from_other_node_rejected():
    other = dict(BLUEPRINT_PACKET, node_id="node-b")
    data = p.encode_signed(other, node_key())  # signed with node A's key
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(data, MASTER)
    assert exc.value.reason == "bad_signature"


def test_unsigned_rejected_unless_allowed():
    data = p.encode_signed(BLUEPRINT_PACKET, None)
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(data, MASTER)
    assert exc.value.reason == "unsigned"
    assert isinstance(p.decode_message(data, MASTER, require_signature=False), p.Telemetry)


def test_microsecond_timestamp_from_blueprint_example_rejected():
    # The blueprint example value 1782345600000000 is microseconds, not ns.
    bad = dict(BLUEPRINT_PACKET, timestamp_ns=1782345600000000)
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(p.encode_signed(bad, node_key()), MASTER)
    assert exc.value.reason == "bad_timestamp"


@pytest.mark.parametrize(
    "path,value,reason",
    [
        (("gps_coordinates", "latitude"), 91.0, "out_of_range"),
        (("vector_target", "elevation_deg"), "high", "invalid_field"),
        (("pixel_metrics", "relative_intensity"), 1.5, "out_of_range"),
        (("node_id",), "bad id with spaces", "invalid_field"),
        (("imu_orientation", "pitch_deg"), True, "invalid_field"),
    ],
)
def test_validation_errors(path, value, reason):
    pkt = json.loads(json.dumps(BLUEPRINT_PACKET))
    target = pkt
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(p.encode_signed(pkt, None), MASTER, require_signature=False)
    assert exc.value.reason == reason


def test_azimuth_normalised():
    pkt = json.loads(json.dumps(BLUEPRINT_PACKET))
    pkt["vector_target"]["azimuth_deg"] = -10.0
    msg = p.decode_message(p.encode_signed(pkt, None), MASTER, require_signature=False)
    assert msg.vector_target.azimuth_deg == pytest.approx(350.0)


def test_heartbeat_round_trip():
    hb = {
        "type": "heartbeat",
        "node_id": "node-a",
        "timestamp_ns": 1782345600000000000,
        "seq": 7,
        "gps_coordinates": BLUEPRINT_PACKET["gps_coordinates"],
        "imu_orientation": BLUEPRINT_PACKET["imu_orientation"],
        "camera": {"width": 1920, "height": 1080, "fps": 60.0, "fov_h_deg": 90.0, "fov_v_deg": 58.7},
        "status": {"fps": 59.9, "mem_used_mb": 3100},
        "software_version": "0.1.0",
    }
    msg = p.decode_message(p.encode_signed(hb, node_key("node-a")), MASTER)
    assert isinstance(msg, p.Heartbeat)
    assert msg.camera.fov_h_deg == 90.0


def test_clip_request_node_side_verification():
    req = p.ClipRequest(
        request_id="r1",
        event_id="e1",
        node_id="node-a",
        t_start_ns=1782345600000000000,
        t_end_ns=1782345605000000000,
        upload_url="https://api.example.org/api/v1/clips/r1/upload-target",
        issued_ns=1782345600100000000,
    )
    key = node_key("node-a")
    data = p.encode_signed(req.to_dict(), key)
    assert len(data) < p.MAX_DATAGRAM_BYTES
    assert p.decode_for_node(data, key) == req
    with pytest.raises(p.ProtocolError):
        p.decode_for_node(data, node_key("node-b"))
    with pytest.raises(p.ProtocolError):
        p.decode_for_node(p.encode_signed(req.to_dict(), None), key)


def test_split_signature_on_unsigned_and_garbage():
    assert p.split_signature(b'{"a":1}') == (b'{"a":1}', None)
    assert p.split_signature(b"") == (b"", None)
    with pytest.raises(p.ProtocolError):
        p.decode_message(b"not json", MASTER)
    with pytest.raises(p.ProtocolError):
        p.decode_message(b"[1,2]", MASTER)
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(b'{"type":"nope","node_id":"a"}', MASTER)
    assert exc.value.reason == "unknown_type"


def test_oversize_rejected():
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_message(b"{" + b" " * 2000 + b"}", MASTER)
    assert exc.value.reason == "too_large"


def test_http_request_signature():
    key = node_key("node-a")
    sig = p.sign_http_request(key, "post", "/api/v1/clips/r1/upload-target", 1782345600000000000, b"{}")
    assert p.verify_http_request(key, "POST", "/api/v1/clips/r1/upload-target", 1782345600000000000, b"{}", sig)
    assert not p.verify_http_request(key, "POST", "/api/v1/clips/r2/upload-target", 1782345600000000000, b"{}", sig)
    digest = hashlib.sha256(b"{}").hexdigest()
    assert p.sign_http_digest(key, "post", "/api/v1/clips/r1/upload-target", 1782345600000000000, digest) == sig
