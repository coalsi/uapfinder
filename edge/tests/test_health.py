import os
import socket

from skynet_edge.health import (SdNotifier, headline_temperatures, parse_chrony_tracking_csv, process_memory_mb,
                                system_memory_mb, thermal_zones_c)


def test_sd_notify_over_a_unix_datagram_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(2)
    n = SdNotifier({"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid())})
    assert n.enabled and n.watchdog_s == 30.0
    assert n.ready("processing")
    assert srv.recv(256) == b"READY=1\nSTATUS=processing"
    assert n.watchdog()
    assert srv.recv(256) == b"WATCHDOG=1"
    assert n.stopping()
    assert srv.recv(256) == b"STOPPING=1"
    srv.close()


def test_sd_notify_abstract_namespace_and_disabled():
    name = f"skynet-test-{os.getpid()}"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0" + name)
    srv.settimeout(2)
    assert SdNotifier({"NOTIFY_SOCKET": "@" + name}).status("hello")
    assert srv.recv(64) == b"STATUS=hello"
    srv.close()
    off = SdNotifier({})
    assert not off.enabled and not off.watchdog() and off.watchdog_s is None
    assert SdNotifier({"WATCHDOG_USEC": "1000", "WATCHDOG_PID": "1"}).watchdog_s is None  # meant for another pid


def test_thermal_zones(tmp_path):
    for i, (name, milli) in enumerate([("cpu-thermal", 48500), ("gpu-thermal", 51250), ("tj-thermal", 52000)]):
        z = tmp_path / f"thermal_zone{i}"
        z.mkdir()
        (z / "type").write_text(name + "\n")
        (z / "temp").write_text(f"{milli}\n")
    (tmp_path / "thermal_zone9").mkdir()  # unreadable zone is skipped
    zones = thermal_zones_c(str(tmp_path))
    assert zones == {"cpu-thermal": 48.5, "gpu-thermal": 51.2, "tj-thermal": 52.0}
    assert headline_temperatures(zones) == {"gpu_temp_c": 51.2, "cpu_temp_c": 48.5, "max_temp_c": 52.0}
    assert headline_temperatures({}) == {}


def test_chrony_csv():
    line = ("50505300,PPS,1,1782345600.123456789,0.000000021,-0.000000004,0.000000120,-1.234,0.001,0.010,"
            "0.000000001,0.000010000,16.0,Normal\n")
    assert parse_chrony_tracking_csv(line) == {"clock_ref": "PPS", "clock_stratum": 1, "clock_offset_ms": 0.0,
                                               "clock_leap": "Normal"}
    line2 = line.replace("0.000000021", "-0.002500000")
    assert parse_chrony_tracking_csv(line2)["clock_offset_ms"] == -2.5
    assert parse_chrony_tracking_csv("garbage") is None


def test_memory_readings():
    m = process_memory_mb()
    assert m["rss_mb"] > 10 and m["peak_rss_mb"] >= m["rss_mb"]
    s = system_memory_mb()
    assert s["total_mb"] > s["available_mb"] > 0
