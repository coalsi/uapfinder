from pathlib import Path

import pytest

from skynet_edge.config import ConfigError, load_config, parse_config, ring_memory_bytes, ring_slots
from skynet_edge.ringbuffer import FrameRing

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example.yaml"


def test_example_config_parses():
    cfg = load_config(EXAMPLE)
    assert cfg.node.node_id == "node-a"
    assert cfg.node.key_file == "/etc/skynet/node.key"
    assert cfg.server.udp_host == "mac-studio" and cfg.server.udp_port == 9999
    assert cfg.server.https_base_url == "http://mac-studio:8000"
    assert cfg.server.report_hz == 10 and cfg.server.heartbeat_interval_s == 10
    assert cfg.camera.width == 1920 and cfg.camera.height == 1080 and cfg.camera.fps == 60
    assert cfg.attitude.bno055.address == 0x28  # YAML hex literal
    assert cfg.gps.static is not None and cfg.gps.static.altitude_m == pytest.approx(357.2)
    assert cfg.detection.sky_mask.min_elevation_deg == 5.0
    assert cfg.adsb.source == "none"


def test_defaults_are_valid_and_pipeline_template_is_filled():
    cfg = parse_config({})
    p = cfg.camera.pipeline()
    assert "nvarguscamerasrc sensor-id=0" in p
    assert "width=1920,height=1080,framerate=60/1" in p
    assert "format=I420" in p and p.rstrip().split("!")[-1].strip().startswith("appsink")
    assert cfg.anomaly.report_unidentified is True


@pytest.mark.parametrize("data, fragment", [
    ({"camera": {"widht": 1280}}, "unknown key"),
    ({"camera": {"width": 1281}}, "even"),
    ({"camera": {"fps": "60"}}, "must be a number"),
    ({"server": {"transport": "carrier-pigeon"}}, "must be one of"),
    ({"server": {"transport": "https"}}, "https_base_url"),
    ({"server": {"https_base_url": "mac-studio:8000"}}, "http://"),
    ({"server": {"heartbeat_interval_s": 90}}, "heartbeat_interval_s"),
    ({"node": {"node_id": "bad id!"}}, "node_id"),
    ({"node": {"key_hex": "abcd"}}, "64 hex"),
    ({"gps": {"source": "static"}}, "static"),
    ({"gps": {"source": "static", "static": {"latitude": 1.0}}}, "missing required"),
    ({"camera": {"source": "file"}}, "file_path"),
    ({"attitude": {"source": "bno055", "bno055": {"address": 0x30}}}, "0x28 or 0x29"),
    ({"detection": {"decimation": 3}}, "decimation"),
    ({"detection": {"sky_mask": {"polygon": [[0, 0], [1, 1]]}}}, "polygon"),
    ({"adsb": {"source": "dump1090"}}, "exactly one of url or file_path"),
    ({"ring_buffer": {"seconds": 3}, "clips": {"pre_s": 5}}, "pre-trigger"),
    ({"tracking": {"confirm_hits": 9, "confirm_window": 8}}, "confirm_hits"),
    ({"anomaly": {"min_anomaly_score": 1.5}}, "min_anomaly_score"),
    ({"health": {"watchdog": "yes"}}, "true or false"),
    ([], "mapping"),
])
def test_invalid_configs_fail_loudly(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_config(data)


def test_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("camera: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p)


def test_key_from_file_and_hex(tmp_path):
    key = "ab" * 32
    f = tmp_path / "node.key"
    f.write_text(key + "\n")
    assert parse_config({"node": {"key_file": str(f)}}).node.load_key() == bytes.fromhex(key)
    assert parse_config({"node": {"key_hex": key}}).node.load_key() == bytes.fromhex(key)
    assert parse_config({}).node.load_key() is None
    with pytest.raises(ConfigError, match="only one"):
        parse_config({"node": {"key_hex": key, "key_file": str(f)}})
    f.write_text("not a key")
    with pytest.raises(ConfigError, match="64 hex"):
        parse_config({"node": {"key_file": str(f)}}).node.load_key()


def test_ring_size_is_checked_against_the_memory_budget():
    cfg = parse_config({})
    window, hold = ring_slots(cfg)
    assert (window, hold) == (600, 240)
    assert ring_memory_bytes(1920, 1080, window + hold) == 1920 * 1080 * 3 // 2 * 840  # ~2.6 GB
    tight = parse_config({"ring_buffer": {"memory_budget_mb": 1000}})
    with pytest.raises(ConfigError, match="memory_budget_mb"):
        FrameRing.from_config(tight)
