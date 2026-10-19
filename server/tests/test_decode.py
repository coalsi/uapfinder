"""The fast decoder accepts and rejects exactly what the reference decoder does."""

from __future__ import annotations

import json
import time

import pytest

from skynet_common.protocol import ProtocolError, decode_message, derive_node_key, encode_signed
from skynet_server import sim
from skynet_server.decode import Decoder

from .conftest import MASTER_KEY

NODE = sim.SimNode("node-a", 33.5, -112.0, 350.0)
KEY = derive_node_key(MASTER_KEY, "node-a")


def telemetry(**changes) -> dict:
    msg = sim.telemetry_dict(NODE, time.time_ns(), 120.0, 45.0, "t-1", seq=5)
    msg.update(changes)
    return msg


def outcome(fn, data):
    try:
        return type(fn(data)).__name__
    except ProtocolError as exc:
        return exc.reason


CASES = [
    encode_signed(telemetry(), KEY),
    encode_signed(sim.heartbeat_dict(NODE, time.time_ns()), KEY),
    encode_signed({k: v for k, v in telemetry().items() if k != "type"}, KEY),  # type defaults to telemetry
    encode_signed(telemetry(), b"wrong-key" * 4),
    encode_signed(telemetry(), None),  # unsigned
    encode_signed(telemetry(type="bogus"), KEY),
    encode_signed(telemetry(node_id="bad id!"), KEY),
    encode_signed(telemetry(timestamp_ns=1782345600000000), KEY),  # microseconds
    encode_signed(telemetry(vector_target={"azimuth_deg": 10.0, "elevation_deg": 120.0}), KEY),
    encode_signed(telemetry(gps_coordinates="here"), KEY),
    encode_signed(telemetry(seq=True), KEY),
    encode_signed(telemetry(track_id="x" * 65), KEY),
    encode_signed(telemetry(pixel_metrics={"angular_velocity_deg_s": 1.0, "relative_intensity": 2.0}), KEY),
    encode_signed(telemetry(notes="x" * 1200), KEY),  # too_large
    b"{not json",
    b"[1, 2]",
    b"",
    encode_signed(telemetry(), KEY)[:-3] + b'0"}',  # tampered signature
    encode_signed(telemetry(), KEY).replace(b'"seq":5', b'"seq":6'),  # tampered body
]


@pytest.mark.parametrize("data", CASES)
def test_matches_reference(data: bytes):
    fast = Decoder(MASTER_KEY)
    assert outcome(fast.decode, data) == outcome(lambda b: decode_message(b, MASTER_KEY), data)


def test_decoded_fields_match_reference():
    data = encode_signed(telemetry(), KEY)
    assert Decoder(MASTER_KEY).decode(data) == decode_message(data, MASTER_KEY)


def test_clip_requests_from_nodes_are_rejected():
    msg = {"type": "clip_request", "request_id": "r", "event_id": "e", "node_id": "node-a",
           "t_start_ns": time.time_ns(), "t_end_ns": time.time_ns(), "upload_url": "u", "issued_ns": time.time_ns()}
    data = encode_signed(msg, KEY)
    assert outcome(lambda b: decode_message(b, MASTER_KEY), data) == "ClipRequest"
    assert outcome(Decoder(MASTER_KEY).decode, data) == "wrong_type"


def test_unsigned_dev_mode_and_missing_key():
    unsigned = encode_signed(telemetry(), None)
    assert outcome(Decoder(MASTER_KEY, require_signature=False).decode, unsigned) == "Telemetry"
    assert outcome(Decoder(None).decode, encode_signed(telemetry(), KEY)) == "no_key"


def test_freshness_window():
    d = Decoder(MASTER_KEY, freshness_ns=10_000_000_000)
    msg = d.decode(encode_signed(telemetry(), KEY))
    now = msg.timestamp_ns
    assert d.is_fresh(msg, now + 9_000_000_000) and d.is_fresh(msg, now - 9_000_000_000)
    assert not d.is_fresh(msg, now + 11_000_000_000)


def test_signature_covers_raw_bytes_not_canonical_json():
    # Reordering members changes the signed bytes, so the signature no longer verifies.
    data = encode_signed(telemetry(), KEY)
    obj = json.loads(data)
    reordered = json.dumps(dict(reversed(list(obj.items()))), separators=(",", ":")).encode()
    assert outcome(Decoder(MASTER_KEY).decode, reordered) in ("unsigned", "bad_signature")
