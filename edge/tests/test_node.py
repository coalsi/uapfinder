import threading
import time

import numpy as np

from helpers import T0_NS, small_config
from skynet_common.protocol import MAX_DATAGRAM_BYTES
from skynet_edge.capture import FrameSource
from skynet_edge.node import EdgeNode


class HungCamera(FrameSource):
    """A live source whose driver delivers a few frames and then blocks forever."""

    live = True

    def __init__(self, w, h):
        super().__init__(w, h, 30.0)
        self.n = 0
        self.release = threading.Event()

    def read(self):
        self.n += 1
        if self.n > 5:
            self.release.wait()
            return None
        time.sleep(1 / 30)
        return np.full((self.height * 3 // 2, self.width), 100, np.uint8), T0_NS + self.n * 33_333_333


def test_stop_returns_even_if_the_camera_hangs(tmp_path):
    cfg = small_config({"clips": {"storage_dir": str(tmp_path)}}, width=64, height=48)
    cam = HungCamera(64, 48)
    node = EdgeNode(cfg, source=cam)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", node.run(drain_timeout_s=1.0)), daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while node.counters["frames"] < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    node.stop()
    t.join(20)
    cam.release.set()
    assert not t.is_alive() and result["code"] == 0 and node.counters["frames"] == 5


def test_heartbeat_is_trimmed_to_one_datagram(tmp_path):
    cfg = small_config({"node": {"key_hex": "11" * 32}, "clips": {"storage_dir": str(tmp_path)}})
    node = EdgeNode(cfg)
    node.pipeline.labels.update({f"label-{i:03d}": i for i in range(200)})  # absurdly large status
    msg = node.heartbeat_message()
    data = node.uplink.encode(msg)
    assert data is not None and len(data) <= MAX_DATAGRAM_BYTES
    assert "filtered" not in msg["status"] and "fps" in msg["status"]


def test_no_heartbeat_without_a_position(tmp_path):
    cfg = small_config({"gps": {"source": "gpsd", "static": None, "gpsd_port": 1},
                        "clips": {"storage_dir": str(tmp_path)}})
    assert EdgeNode(cfg).heartbeat_message() is None
