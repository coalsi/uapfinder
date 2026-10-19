import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

from helpers import T0_NS, small_config
from skynet_edge.capture import (CaptureThread, FrameQueue, FrameRef, OpenCVSource, PipeSource, RenderedSource,
                                 gst_launch_command, make_source, opencv_has_gstreamer)
from skynet_edge.config import parse_config
from skynet_edge.ringbuffer import FrameRing

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def test_gst_launch_command_replaces_appsink():
    argv = gst_launch_command(parse_config({}).camera)
    assert argv[:2] == ["gst-launch-1.0", "-q"]
    assert "nvarguscamerasrc" in argv and "sensor-id=0" in argv
    assert "video/x-raw(memory:NVMM),width=1920,height=1080,framerate=60/1,format=NV12" in argv
    assert "appsink" not in " ".join(argv)
    assert argv[-3:] == ["fdsink", "fd=1", "sync=false"] and "leaky=downstream" in argv


def test_make_source_selection():
    cfg = parse_config({"camera": {"gstreamer_backend": "subprocess"}})
    assert isinstance(make_source(cfg), PipeSource)
    auto = make_source(parse_config({}))
    assert isinstance(auto, OpenCVSource if opencv_has_gstreamer() else PipeSource)
    assert isinstance(make_source(small_config(), n_frames=3), RenderedSource)
    assert isinstance(make_source(parse_config({"camera": {"source": "v4l2"}})), OpenCVSource)


@needs_ffmpeg
def test_pipe_source_reads_whole_i420_frames():
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=30",
            "-frames:v", "5", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    src = PipeSource(argv, 64, 48, 30.0, latency_ms=20.0)
    src.open()
    frames, stamps = [], []
    while (got := src.read()) is not None:
        frames.append(got[0].copy())
        stamps.append(got[1])
    src.close()
    assert len(frames) == 5 and frames[0].shape == (72, 64)
    assert frames[0][:48].std() > 10  # a real test pattern, not zeros
    assert all(b >= a for a, b in zip(stamps, stamps[1:])) and stamps[0] < time.time_ns() - 19_000_000


@needs_ffmpeg
def test_video_file_source_timestamps_and_loop(tmp_path):
    path = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=30", "-frames:v", "6", "-pix_fmt", "yuv420p", str(path)], check=True)
    cam = parse_config({"camera": {"source": "file", "file_path": str(path), "width": 64, "height": 48, "fps": 30,
                                   "file_start_ns": T0_NS, "file_loop": True, "realtime": False}}).camera
    src = OpenCVSource(cam)
    assert not src.live
    src.open()
    got = [src.read() for _ in range(9)]
    src.close()
    assert all(f.shape == (72, 64) for f, _ in got)
    assert [t for _, t in got] == [T0_NS + round(i * 1e9 / 30) for i in range(9)]  # continues across the loop


def test_frame_queue_drop_and_block():
    q = FrameQueue(2)
    for i in range(4):
        q.put(FrameRef(i, i))
    assert q.dropped == 2 and q.get().seq == 2
    b = FrameQueue(1, block=True)
    b.put(FrameRef(0, 0))
    t = threading.Thread(target=b.put, args=(FrameRef(1, 1),))
    t.start()
    time.sleep(0.05)
    assert t.is_alive()  # waits for the consumer instead of dropping
    assert b.get().seq == 0
    t.join(1)
    assert b.get().seq == 1 and b.dropped == 0
    b.close()
    assert b.get(timeout=0.01) is None and b.finished()


def test_capture_thread_fills_ring_and_queue():
    w, h = 32, 24
    src = RenderedSource(w, h, 30.0, lambda i, t: np.full((h * 3 // 2, w), i, np.uint8), T0_NS, n_frames=12)
    ring = FrameRing(w, h, window_frames=20, prefault=False)
    q = FrameQueue(4, block=True)
    th = CaptureThread(src, ring, q, threading.Event())
    th.start()
    seen = []
    while not q.finished():
        ref = q.get(timeout=0.5)
        if ref is not None:
            seen.append((ref.seq, ref.t_ns, int(ring.y_plane(ref.seq)[0, 0])))
    th.join(2)
    assert th.error is None and th.frames == 12
    assert seen == [(i, src.frame_time_ns(i), i) for i in range(12)]


def test_capture_thread_reports_offline_source_errors():
    class Broken(RenderedSource):
        def open(self):
            raise OSError("no such file")

    src = Broken(32, 24, 30.0, lambda i, t: None, T0_NS)
    th = CaptureThread(src, FrameRing(32, 24, 4, prefault=False), FrameQueue(4, True), threading.Event())
    th.run()
    assert isinstance(th.error, OSError)
