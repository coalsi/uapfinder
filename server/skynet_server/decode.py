"""Message authentication and validation on the ingest hot path.

This is ``skynet_common.protocol.decode_message`` restructured for throughput,
with identical accept/reject decisions and reasons (tests/test_decode.py
checks them against the reference on valid and malformed inputs):

* node keys are derived once per node and cached instead of per message
  (an HMAC each time);
* JSON is parsed with orjson (~5x faster than the stdlib on these messages);
* the HMAC is checked with the one-shot ``hmac.digest``.

The model classes (``Telemetry.from_dict`` / ``Heartbeat.from_dict``) are the
shared ones, so field validation cannot drift from the protocol package.
Messages a node must never send (``clip_request``) are rejected as
``wrong_type``. Freshness is a separate check (``is_fresh``) because a stale
message is still authenticated: its node may collect pending commands.
"""

from __future__ import annotations

import hmac
import re
from typing import Dict, Optional, Union

import orjson
from skynet_common.protocol import (
    MAX_DATAGRAM_BYTES,
    MSG_CLIP_REQUEST,
    MSG_HEARTBEAT,
    MSG_TELEMETRY,
    Heartbeat,
    ProtocolError,
    Telemetry,
    derive_node_key,
    split_signature,
)

Message = Union[Telemetry, Heartbeat]

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # protocol.py's node id rule
_KEY_CACHE_LIMIT = 100_000


class Decoder:
    def __init__(self, master_key: Optional[bytes], require_signature: bool = True, freshness_ns: int = 10_000_000_000):
        self.master_key = master_key
        self.require_signature = require_signature
        self.freshness_ns = freshness_ns
        self._keys: Dict[str, bytes] = {}

    def node_key(self, node_id: str) -> bytes:
        key = self._keys.get(node_id)
        if key is None:
            if self.master_key is None:
                raise ProtocolError("no_key", "server has no master key configured")
            if len(self._keys) >= _KEY_CACHE_LIMIT:
                self._keys.clear()
            key = self._keys[node_id] = derive_node_key(self.master_key, node_id)
        return key

    def decode(self, data: bytes) -> Message:
        if len(data) > MAX_DATAGRAM_BYTES:
            raise ProtocolError("too_large", f"{len(data)} bytes > {MAX_DATAGRAM_BYTES}")
        try:
            obj = orjson.loads(data)
        except orjson.JSONDecodeError as exc:
            raise ProtocolError("bad_json", str(exc)) from None
        if not isinstance(obj, dict):
            raise ProtocolError("bad_json", "message must be a JSON object")
        msg_type = obj.get("type", MSG_TELEMETRY)
        if msg_type == MSG_TELEMETRY:
            model = Telemetry
        elif msg_type == MSG_HEARTBEAT:
            model = Heartbeat
        elif msg_type == MSG_CLIP_REQUEST:
            raise ProtocolError("wrong_type", "clip_request is a server-to-node message")
        else:
            raise ProtocolError("unknown_type", str(msg_type))
        node_id = obj.get("node_id")
        if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
            raise ProtocolError("invalid_field", "node_id must be 1-64 chars of [A-Za-z0-9_.:-]")
        body, sig = split_signature(data)
        if sig is None:
            if self.require_signature:
                raise ProtocolError("unsigned")
        elif self.master_key is not None:
            expected = hmac.digest(self.node_key(node_id), body, "sha256").hex().encode("ascii")
            if not hmac.compare_digest(expected, sig):
                raise ProtocolError("bad_signature")
        elif self.require_signature:
            raise ProtocolError("no_key", "server has no master key configured")
        return model.from_dict(obj)

    def is_fresh(self, msg: Message, now_ns: int) -> bool:
        return abs(now_ns - msg.timestamp_ns) <= self.freshness_ns
