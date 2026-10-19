"""End to end: synthetic sky -> EdgeNode -> UDP -> fake server -> clip_request -> clip upload.

The rendered scene (see ``skynet_edge.synthetic``) has a point target that
makes a hard turn, a slow diffuse cloud, a flapping bird close to the lens
and an airliner with a matching ADS-B record. Only the target may be
reported, and every reported direction must match the ground truth.
"""

import hashlib
import json
import shutil
import time

import cv2
import numpy as np
import pytest

from fakeserver import FakeHttpServer, FakeUdpServer, clip_request
from helpers import MASTER_KEY, T0_NS, small_config
from skynet_common.protocol import Heartbeat, Telemetry, derive_node_key
from skynet_edge.capture import RenderedSource
from skynet_edge.geometry import angular_separation_deg
from skynet_edge.node import EdgeNode
from skynet_edge.synthetic import standard_scene

NODE = "node-test"
FPS = 30.0
SECONDS = 8.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_synthetic_sky_end_to_end(tmp_path):
    udp = FakeUdpServer(MASTER_KEY)
    http = FakeHttpServer(MASTER_KEY)
    try:
        _run(tmp_path, udp, http)
    finally:
        udp.close()
        http.close()


def _run(tmp_path, udp, http):
    adsb_file = tmp_path / "aircraft.json"
    cfg = small_config({
        "node": {"node_id": NODE, "key_hex": derive_node_key(MASTER_KEY, NODE).hex()},
        "server": {"transport": "udp", "udp_host": "localhost", "udp_port": udp.port,
                   "https_base_url": http.base, "heartbeat_interval_s": 1.0},
        "adsb": {"source": "dump1090", "file_path": str(adsb_file), "max_position_age_s": 30.0},
        "clips": {"storage_dir": str(tmp_path / "clips"), "record_events": True},
    }, fps=FPS)
    scene = standard_scene(cfg, t0_ns=T0_NS, loop=False, duration_s=SECONDS)
    adsb_file.write_text(json.dumps(scene.adsb_json(T0_NS)))
    source = RenderedSource(cfg.camera.width, cfg.camera.height, FPS, scene.render, T0_NS,
                            n_frames=int(SECONDS * FPS))

    requested = {}

    def on_message(msg, peer):
        if isinstance(msg, Telemetry) and not requested:
            requested.update(t=msg.timestamp_ns)
            # The back-signal, as a server without SKYNET_PUBLIC_URL sends it (loopback upload URL),
            # plus a forged one that must be ignored.
            url = "http://127.0.0.1:1/api/v1/clips/req-e2e/upload-target"
            udp.send(clip_request(MASTER_KEY, NODE, "req-e2e", msg.timestamp_ns - int(1e9),
                                  msg.timestamp_ns + int(1e9), url), peer)
            udp.send(clip_request(MASTER_KEY, NODE, "req-forged", msg.timestamp_ns, msg.timestamp_ns + 1, url,
                                  sign_with=b"f" * 32), peer)

    udp.on_message = on_message
    node = EdgeNode(cfg, source=source)
    assert node.run(drain_timeout_s=60.0) == 0

    # --- what was reported ---------------------------------------------------
    deadline = time.monotonic() + 2
    while not udp.of_type("heartbeat") and time.monotonic() < deadline:
        time.sleep(0.05)
    telemetry = udp.of_type("telemetry")
    assert not udp.rejected
    assert telemetry, f"nothing reported; filtered {dict(node.pipeline.labels)}"
    errors, speeds = [], []
    for t in telemetry:
        truth = {name: scene.truth(name, t.timestamp_ns) for name in ("target", "cloud", "bird", "aircraft")}
        sep = {k: angular_separation_deg(t.vector_target.azimuth_deg, t.vector_target.elevation_deg, *v)
               for k, v in truth.items() if v is not None}
        assert min(sep, key=sep.get) == "target", f"reported a {min(sep, key=sep.get)}: {sep}"
        errors.append(sep["target"])
        speeds.append(t.pixel_metrics.angular_velocity_deg_s)
        assert t.node_id == NODE and t.imu_orientation.yaw_deg == pytest.approx(200.0)
        assert t.gps_coordinates.latitude == pytest.approx(33.6292)
    assert max(errors) < 0.1, f"max direction error {max(errors):.4f} deg"
    assert np.median(errors) < 0.02
    assert len({t.track_id for t in telemetry}) == 1  # one object, one track
    assert max(t.anomaly_score for t in telemetry) > 0.5  # the hard turn
    target_speed = 0.06 * node.cam.fov_h_deg
    assert np.median(speeds) == pytest.approx(target_speed, rel=0.1)

    # Report cadence: report_hz (10 Hz), starting at the beginning of the track (backfill).
    ts = np.array(sorted(t.timestamp_ns for t in telemetry)) / 1e9 - T0_NS / 1e9
    gaps = np.diff(ts)
    assert np.all(gaps > 0.09) and np.median(gaps) == pytest.approx(0.1, abs=0.034)
    assert ts[0] < 0.3 + 0.4  # the target appears at 0.3 s
    assert len(telemetry) > 50

    # Everything else was explained by a filter.
    labels = dict(node.pipeline.labels)
    for label in ("cloud", "bird", "aircraft"):
        assert labels.get(label, 0) >= 1, labels
    assert node.pipeline.reported_tracks == 1

    hb = udp.of_type("heartbeat")
    assert hb and isinstance(hb[0], Heartbeat)
    assert hb[0].camera.width == 640 and hb[0].camera.fov_h_deg == pytest.approx(60.0)
    assert "stage_ms" in hb[-1].status or "tracks_active" in hb[-1].status

    # --- the clip ---------------------------------------------------------------
    assert node.uplink.stats["bad_commands"] == 1  # the forged request
    assert node.uploader.outcomes["req-e2e"].ok, node.uploader.outcomes["req-e2e"].error
    body = http.uploads["req-e2e"]
    done = http.completes["req-e2e"]
    assert done["sha256"] == hashlib.sha256(body).hexdigest() and done["bytes"] == len(body)
    # The window may start before the first frame of the video; the clip then starts at that frame.
    assert done["t_start_ns"] <= max(T0_NS, requested["t"] - int(1e9))
    assert done["t_end_ns"] >= requested["t"] + int(1e9)
    mp4 = tmp_path / "uploaded.mp4"
    mp4.write_bytes(body)
    cap = cv2.VideoCapture(str(mp4))
    frames = 0
    while cap.read()[0]:
        frames += 1
    # Served from the event recording (most of the video) or, if the request beat the event's start,
    # straight from the ring buffer (exactly the requested window): either way it covers the request.
    covered_s = (requested["t"] + 1e9 - max(T0_NS, requested["t"] - 1e9)) / 1e9
    assert frames == done["frames"] >= int(covered_s * FPS)
    assert not http.auth_failures
    assert node.ring.overruns == 0
