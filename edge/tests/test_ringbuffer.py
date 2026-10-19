import json
import os
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from helpers import T0_NS, small_config
from skynet_edge.clips import ClipManager, Y4MWriter, ffmpeg_command, safe_name
from skynet_edge.ringbuffer import FrameRing

W, H, FPS = 64, 48, 30.0
DT = int(1e9 / FPS)
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def frame(i: int) -> np.ndarray:
    f = np.full((H * 3 // 2, W), 128, dtype=np.uint8)
    f[:H] = i % 256
    return f


def fill(ring: FrameRing, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        ring.write(frame(i), T0_NS + i * DT)


def test_indexing_and_eviction():
    ring = FrameRing(W, H, window_frames=10, hold_frames=0, prefault=True)
    assert ring.frame_shape == (72, 64) and ring.nbytes == 10 * 64 * 72
    fill(ring, 25)
    assert ring.latest() == (24, T0_NS + 24 * DT)
    assert ring.oldest_time_ns() == T0_NS + 15 * DT
    assert ring.y_plane(14) is None and int(ring.y_plane(20)[0, 0]) == 20
    assert [s for s, _ in ring.frames_between(T0_NS + 17 * DT, T0_NS + 19 * DT)] == [17, 18, 19]
    out = np.empty(ring.frame_shape, np.uint8)
    assert ring.copy_frame(22, out) == T0_NS + 22 * DT and int(out[0, 0]) == 22
    with pytest.raises(ValueError):
        ring.write(np.zeros((10, 10), np.uint8), T0_NS)


def test_protection_holds_frames_without_blocking_the_writer():
    ring = FrameRing(W, H, window_frames=10, hold_frames=8, prefault=False)
    fill(ring, 10)
    p = ring.protect(T0_NS + 2 * DT, T0_NS + 6 * DT)
    fill(ring, 10, start=10)  # the window has moved on; frames 2..6 are held, not overwritten
    assert ring.stats()["held"] == 5
    assert [s for s, _ in ring.frames_between(T0_NS, T0_NS + 9 * DT)] == [2, 3, 4, 5, 6]
    out = np.empty(ring.frame_shape, np.uint8)
    assert ring.copy_frame(4, out) is not None and int(out[0, 0]) == 4
    ring.advance(p, T0_NS + 4 * DT)  # exported up to frame 4: those slots are freed
    assert ring.stats()["held"] == 2
    ring.release(p)
    assert ring.stats()["held"] == 0 and ring.overruns == 0


def test_hold_pool_overrun_sacrifices_oldest_held_frame():
    ring = FrameRing(W, H, window_frames=4, hold_frames=2, prefault=False)
    fill(ring, 4)
    ring.protect(T0_NS, T0_NS + 3 * DT)
    fill(ring, 3, start=4)  # 3 protected frames leave the window; only 2 spare slots
    assert ring.overruns == 1
    assert [s for s, _ in ring.frames_between(T0_NS, T0_NS + 3 * DT)] == [1, 2, 3]


def test_copy_detects_overwrite_during_copy():
    ring = FrameRing(W, H, window_frames=2, hold_frames=0, prefault=False)
    fill(ring, 2)
    out = np.empty(ring.frame_shape, np.uint8)
    orig = np.copyto

    def racing_copy(dst, src, *a, **k):  # the writer reuses slot 0 while the exporter copies it
        orig(dst, src, *a, **k)
        if dst is out:
            np.copyto = orig
            fill(ring, 1, start=2)

    np.copyto = racing_copy
    try:
        assert ring.copy_frame(0, out) is None
    finally:
        np.copyto = orig


def test_wait_for_frame():
    ring = FrameRing(W, H, window_frames=4, prefault=False)
    assert not ring.wait_for_frame(0, 0.01)
    threading.Timer(0.05, lambda: fill(ring, 1)).start()
    assert ring.wait_for_frame(0, 2.0)


def test_y4m_writer(tmp_path):
    p = tmp_path / "x.y4m"
    w = Y4MWriter(p, W, H, 59.94)
    w.write(frame(1))
    w.write(frame(2))
    w.close()
    data = p.read_bytes()
    header = data.split(b"\n", 1)[0]
    assert header == b"YUV4MPEG2 W64 H48 F2997:50 Ip A1:1 C420mpeg2"
    assert len(data) == len(header) + 1 + 2 * (6 + W * H * 3 // 2)


def test_ffmpeg_command_is_low_priority():
    cfg = small_config().clips
    cmd = ffmpeg_command(cfg, Path("in.y4m"), Path("out.mp4"))
    assert "libx264" in cmd and "-threads" in cmd and cmd[-1] == "out.mp4"
    if shutil.which("nice"):
        assert cmd[:3] == ["nice", "-n", "19"]


def test_safe_name():
    assert safe_name("../../etc/passwd") == "_.._etc_passwd"
    assert safe_name("c7d1-req_1.a") == "c7d1-req_1.a"
    assert safe_name("") == "clip"


def _manager(tmp_path, ring, **clips):
    cfg = small_config({"clips": {"storage_dir": str(tmp_path / "clips"), "frame_wait_timeout_s": 2.0, **clips}})
    mgr = ClipManager(ring, cfg.clips, FPS)
    mgr.start()
    return mgr


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_request_clip_from_ring_is_encoded_to_mp4(tmp_path):
    ring = FrameRing(W, H, window_frames=60, hold_frames=30, prefault=False)
    mgr = _manager(tmp_path, ring)
    fill(ring, 30)
    results = []
    done = threading.Event()
    mgr.request_clip("req/1", T0_NS + 10 * DT, T0_NS + 40 * DT, lambda r: (results.append(r), done.set()))
    fill(ring, 20, start=30)  # the request extends into the future: frames 30..40 arrive later
    assert done.wait(30)
    mgr.stop()
    (res,) = results
    assert res.ok, res.error
    assert res.clip_id == "req_1" and res.frames == 31 and res.lost_frames == 0
    assert res.first_ns == T0_NS + 10 * DT and res.last_ns == T0_NS + 40 * DT
    cap = cv2.VideoCapture(res.mp4_path)
    n = 0
    while cap.read()[0]:
        n += 1
    assert n == 31
    meta = json.loads((Path(res.directory) / "meta.json").read_text())
    assert meta["frame_timestamps_ns"][0] == T0_NS + 10 * DT and len(meta["frame_timestamps_ns"]) == 31
    assert not (Path(res.directory) / "clip.y4m").exists()  # raw export removed after encoding
    assert ring.stats()["protections"] == 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_event_clip_serves_later_requests_and_survives_restart(tmp_path):
    ring = FrameRing(W, H, window_frames=60, hold_frames=30, prefault=False)
    mgr = _manager(tmp_path, ring)
    fill(ring, 10)
    mgr.start_event("ev-1", T0_NS, T0_NS + 20 * DT, {"tracks": ["t-1"]})
    mgr.extend_event("ev-1", T0_NS + 30 * DT)
    fill(ring, 31, start=10)
    deadline = time.monotonic() + 30
    while not mgr.stored_clips() and time.monotonic() < deadline:
        time.sleep(0.05)
    (stored,) = mgr.stored_clips()
    assert stored.frames == 31
    got = []
    mgr.request_clip("req-2", T0_NS + 5 * DT, T0_NS + 25 * DT, got.append)
    assert got and got[0].clip_id == "ev-1"  # served immediately from the stored event
    mgr.stop()
    again = _manager(tmp_path, FrameRing(W, H, window_frames=4, prefault=False))
    assert [c.clip_id for c in again.stored_clips()] == ["ev-1"]
    again.stop()


def test_request_for_window_no_longer_buffered_fails_fast(tmp_path):
    ring = FrameRing(W, H, window_frames=10, prefault=False)
    mgr = _manager(tmp_path, ring)
    fill(ring, 30)
    got = []
    mgr.request_clip("old", T0_NS, T0_NS + 5 * DT, got.append)
    mgr.stop()
    assert len(got) == 1 and not got[0].ok and "no longer buffered" in got[0].error


def test_retention_deletes_oldest_clips(tmp_path):
    ring = FrameRing(W, H, window_frames=4, prefault=False)
    mgr = _manager(tmp_path, ring, retention_max_gb=2.5e-6)  # 2.5 kB
    root = tmp_path / "clips" / "events"
    for i, name in enumerate(["a", "b", "c"]):
        d = root / name
        d.mkdir(parents=True)
        (d / "clip.mp4").write_bytes(b"x" * 1000)
        t = time.time() - 100 + i
        os.utime(d, (t, t))
    mgr.enforce_retention()
    mgr.stop()
    assert sorted(p.name for p in root.iterdir()) == ["b", "c"]
