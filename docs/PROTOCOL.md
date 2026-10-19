# SKY-NET wire protocol (v1)

Reference implementation: [`common/skynet_common/protocol.py`](../common/skynet_common/protocol.py).

## Transports

Every message is one compact JSON object (no whitespace, UTF-8, at most
1200 bytes so a UDP datagram is never fragmented). The same bytes can travel
two ways:

| Transport | Node → server | Server → node | Where it works |
|-----------|---------------|---------------|----------------|
| **UDP** | one message per datagram to `udp://<server>:9999` | `clip_request` datagrams sent back to the source address/port of the node's most recent datagram (heartbeats every 10 s keep NAT mappings open) | Self-hosted server (VM / container). Lowest latency; required for the 10k msg/s, <20 ms back-signal target. |
| **HTTPS** | `POST /api/v1/ingest` with `Content-Type: application/x-ndjson`, one signed message per line, batched (default every 100 ms) | pending `clip_request` messages returned in the ingest response, or polled with `GET /api/v1/nodes/{node_id}/commands` | Everywhere, including Vercel (Vercel Functions cannot receive UDP). |

## Signing

Each node has a 32-byte key derived from the server master key, so the server
stores no per-node secrets:

```
node_key = HMAC-SHA256(master_key, "skynet-node-key:" + node_id)
```

To sign, serialise the message **without** a `sig` member, compute
`HMAC-SHA256(node_key, body)` and append it as the last member:

```
{"node_id":"…",…,"pixel_metrics":{…},"sig":"<64 lowercase hex chars>"}
```

The receiver strips the fixed-length `,"sig":"<hex>"` suffix to recover the
exact signed bytes, so no JSON canonicalisation is involved. Messages from the
server (`clip_request`) are signed with the destination node's key; nodes must
reject unsigned or mis-signed server messages.

Node-authenticated HTTP requests (command polling, clip upload) carry:

```
X-Skynet-Node:      <node_id>
X-Skynet-Timestamp: <unix ns>          # must be within ±60 s of server time
X-Skynet-Signature: hex(HMAC-SHA256(node_key, METHOD "\n" PATH "\n" TIMESTAMP "\n" hex(SHA256(body))))
```

`PATH` is the URL path without the query string.

## Message: `telemetry` (node → server)

The blueprint schema plus four optional extension fields. A message without
`type` is treated as telemetry.

```json
{
  "type": "telemetry",
  "node_id": "4f9a2c1e-7b3d-4e8a-9c6f-1a2b3c4d5e6f",
  "gps_coordinates": {"latitude": 33.6292, "longitude": -112.3679, "altitude_m": 357.2},
  "imu_orientation": {"pitch_deg": 45.0, "roll_deg": 0.0, "yaw_deg": 0.0},
  "timestamp_ns": 1782345600000000000,
  "vector_target": {"azimuth_deg": 142.521, "elevation_deg": 65.104},
  "pixel_metrics": {"angular_velocity_deg_s": 12.4, "relative_intensity": 0.85},
  "seq": 1042,
  "track_id": "t-000173",
  "anomaly_score": 0.91,
  "sig": "…"
}
```

| Field | Meaning |
|-------|---------|
| `node_id` | 1–64 chars of `[A-Za-z0-9_.:-]` (UUIDs fit). |
| `gps_coordinates.altitude_m` | Height above the **WGS84 ellipsoid** (HAE), not MSL. NMEA GGA altitude is MSL; add the GGA geoid separation. |
| `imu_orientation` | Attitude of the camera optical axis: `yaw_deg` = azimuth clockwise from **true** north (correct magnetometers for declination), `pitch_deg` = elevation above the horizon, `roll_deg` = rotation about the optical axis, positive = right side of the image down. |
| `timestamp_ns` | Capture time of the frame, **nanoseconds** since the Unix epoch (UTC), from a GPS/PPS-disciplined clock. Always 19 digits for current dates. ⚠️ The blueprint example `1782345600000000` is in microseconds; the server rejects values outside 2001–2096 as `bad_timestamp`. |
| `vector_target` | Line of sight to the object: azimuth clockwise from true north, elevation above the local geodetic horizon, after lens undistortion and attitude correction. |
| `pixel_metrics.angular_velocity_deg_s` | Apparent angular speed of the track. |
| `pixel_metrics.relative_intensity` | 0–1 brightness relative to the frame's dynamic range. |
| `seq` | Per-node message counter. With the timestamp freshness window it lets the server drop replays and duplicates. |
| `track_id` | The node's local track id. Successive telemetry for one object shares it, so the server can link 3D points into a track and interpolate between samples. |
| `anomaly_score` | 0–1 edge-side kinematic score (angular acceleration, turn rate). Informational: true speed and acceleration are only known after triangulation. |

Nodes send telemetry at `report_hz` (default 10 Hz) for as long as a track
that survived the false-positive filters stays visible.

## Message: `heartbeat` (node → server)

Sent every 10 s. Keeps the node in the active spatial index (a node is
*active* if its last heartbeat or telemetry is younger than 60 s), refreshes
the UDP return address, and carries the camera geometry used for coverage
maps and viewing frustums.

```json
{
  "type": "heartbeat",
  "node_id": "node-a",
  "timestamp_ns": 1782345600000000000,
  "seq": 7,
  "gps_coordinates": {"latitude": 33.6292, "longitude": -112.3679, "altitude_m": 357.2},
  "imu_orientation": {"pitch_deg": 45.0, "roll_deg": 0.0, "yaw_deg": 0.0},
  "camera": {"width": 1920, "height": 1080, "fps": 60.0, "fov_h_deg": 90.0, "fov_v_deg": 58.7},
  "status": {"fps": 59.8, "mem_used_mb": 3120, "gpu_temp_c": 51.5, "tracks_active": 2, "clock_offset_ms": 0.02},
  "software_version": "0.1.0",
  "sig": "…"
}
```

`status` is free-form; the keys above are the ones the dashboard displays.

## Message: `clip_request` (server → node)

The back-signal sent when a stereo match is confirmed, asking each
contributing node for its HD video around the event.

```json
{
  "type": "clip_request",
  "request_id": "c7d1…",
  "event_id": "e93a…",
  "node_id": "node-a",
  "t_start_ns": 1782345595000000000,
  "t_end_ns": 1782345605000000000,
  "upload_url": "https://api.example.org/api/v1/clips/c7d1…/upload-target",
  "issued_ns": 1782345600012000000,
  "sig": "…"
}
```

`upload_url` is not the storage location. The node sends a node-authenticated
`POST` to it and receives `{"method": "PUT", "url": "…", "headers": {…}}`,
which is either the server's own clip endpoint (self-hosted) or a presigned
object-storage URL (Vercel deployments, where function request bodies are
limited to a few MB). After uploading, the node calls
`POST /api/v1/clips/{request_id}/complete`. See [API.md](API.md).

## Validation and rejection reasons

The server drops invalid messages and counts them by reason in
`GET /api/v1/stats`: `too_large`, `bad_json`, `unknown_type`, `wrong_type`,
`invalid_field`, `out_of_range`, `bad_timestamp`, `unsigned`,
`bad_signature`, `no_key`, `stale` (timestamp more than 10 s from server
time), `replay` (duplicate `node_id`+`seq`).
