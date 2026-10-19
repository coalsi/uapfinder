import json
import socket
import threading
import time

import pytest

from fakeserver import HTTP_PORTS, bind_free
from skynet_edge.config import parse_config
from skynet_edge.sensors.gps import (GpsdClient, NmeaParser, NmeaSerialGps, PositionSource, make_position_source,
                                     nmea_checksum_ok, parse_gpsd_tpv)


def nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n"


# The classic NMEA reference sentences, with their published checksums.
GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"


def test_checksum():
    assert nmea_checksum_ok(GGA) and nmea_checksum_ok(RMC)
    assert not nmea_checksum_ok(GGA.replace("545.4", "545.5"))
    assert not nmea_checksum_ok("GPGGA,no,dollar*00")
    assert not nmea_checksum_ok("$GPGGA,no,checksum")
    assert nmea(GGA[1:-3]).strip() == GGA


def test_gga_gives_hae_from_msl_plus_geoid_separation():
    p = NmeaParser()
    assert p.feed(RMC) is None  # RMC only sets validity
    fix = p.feed(GGA)
    assert fix["lat"] == pytest.approx(48 + 7.038 / 60)
    assert fix["lon"] == pytest.approx(11 + 31.0 / 60)
    assert fix["alt"] == pytest.approx(545.4 + 46.9)
    assert fix["mode"] == 3


def test_southern_western_hemispheres_and_any_talker():
    s = nmea("GNGGA,010203.00,3337.75200,S,11222.07400,W,2,12,0.7,357.2,M,-27.5,M,,")
    fix = NmeaParser().feed(s)
    assert fix["lat"] == pytest.approx(-(33 + 37.752 / 60))
    assert fix["lon"] == pytest.approx(-(112 + 22.074 / 60))
    assert fix["alt"] == pytest.approx(357.2 - 27.5)


def test_invalid_fixes_are_ignored():
    p = NmeaParser()
    assert p.feed(nmea("GPGGA,123519,4807.038,N,01131.000,E,0,00,,,M,,M,,")) is None  # quality 0
    assert p.feed(GGA.replace("545.4", "545.5")) is None  # bad checksum
    assert p.feed(nmea("GPRMC,123519,V,,,,,,,230394,,")) is None
    assert p.feed(GGA) is None  # RMC said 'V' (void)
    p.feed(RMC)
    assert p.feed(GGA) is not None
    no_sep = nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,,M,,")
    assert p.feed(no_sep)["mode"] == 2 and p.feed(no_sep)["alt"] is None


def test_serial_source_keeps_last_3d_altitude_for_2d_fixes():
    cfg = parse_config({"gps": {"source": "nmea"}}).gps
    src = NmeaSerialGps(cfg)
    parser = NmeaParser()
    src.feed(parser, nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,,M,,"))
    assert src.current() is None  # 2-D fix and no altitude known yet
    src.feed(parser, GGA)
    src.feed(parser, nmea("GPGGA,123520,4807.100,N,01131.000,E,1,08,0.9,545.4,M,,M,,"))
    fix = src.current()
    assert fix.mode == 2 and fix.altitude_m == pytest.approx(592.3) and fix.latitude > 48.1183


def test_gpsd_tpv_parsing():
    assert parse_gpsd_tpv({"class": "TPV", "mode": 3, "lat": 1.5, "lon": 2.5, "altHAE": 100.0, "altMSL": 80.0,
                           "geoidSep": 20.0})["alt"] == 100.0
    assert parse_gpsd_tpv({"class": "TPV", "mode": 3, "lat": 1.5, "lon": 2.5, "altMSL": 80.0,
                           "geoidSep": 20.5})["alt"] == 100.5
    assert parse_gpsd_tpv({"class": "TPV", "mode": 3, "lat": 1.5, "lon": 2.5, "alt": 80.0,
                           "geoidSep": 20.0})["alt"] == 100.0  # pre-3.20 gpsd
    assert parse_gpsd_tpv({"class": "TPV", "mode": 3, "lat": 1.5, "lon": 2.5, "altMSL": 80.0})["alt"] is None
    assert parse_gpsd_tpv({"class": "TPV", "mode": 1, "lat": 1.5, "lon": 2.5}) is None
    assert parse_gpsd_tpv({"class": "SKY", "satellites": []}) is None


def test_fix_age_and_static_fallback():
    now = [1_000_000_000_000]
    cfg = parse_config({"gps": {"source": "gpsd", "max_fix_age_s": 5,
                                "static": {"latitude": 10.0, "longitude": 20.0, "altitude_m": 30.0}}}).gps
    src = PositionSource(cfg, clock=lambda: now[0])
    assert src.current().source == "static"
    src.update(1.0, 2.0, 3.0, 3, "gpsd")
    assert src.current().source == "gpsd"
    now[0] += int(6e9)
    assert src.current().source == "static"
    assert src.status() == {"gps": "static", "gps_mode": 0}


def test_gpsd_client_against_a_fake_gpsd():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = bind_free(srv, HTTP_PORTS)
    srv.listen(1)
    got = {}

    def serve():
        conn, _ = srv.accept()
        with conn:
            got["watch"] = conn.recv(256)
            for obj in ({"class": "VERSION", "release": "3.22"}, {"class": "TPV", "mode": 1},
                        {"class": "TPV", "mode": 3, "lat": 33.6292, "lon": -112.3679, "altHAE": 330.0}):
                conn.sendall(json.dumps(obj).encode() + b"\n")
            time.sleep(1.0)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cfg = parse_config({"gps": {"source": "gpsd", "gpsd_port": port}}).gps
    client = make_position_source(cfg)
    assert isinstance(client, GpsdClient)
    client.start()
    deadline = time.monotonic() + 5
    while client.current() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    srv.close()
    fix = client.current()
    assert got["watch"] == b'?WATCH={"enable":true,"json":true}\n'
    assert fix is not None and fix.mode == 3 and fix.altitude_m == 330.0 and fix.latitude == 33.6292
