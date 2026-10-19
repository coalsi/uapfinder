import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fakeserver import HTTP_PORTS
from helpers import OBSERVER, T0_NS
from skynet_common.geodesy import haversine_m, look_angles
from skynet_edge.adsb import FT_TO_M, KT_TO_MPS, AdsbFeed, Aircraft, destination, parse_aircraft_json
from skynet_edge.config import parse_config

READSB = {
    "now": T0_NS / 1e9,
    "messages": 123,
    "aircraft": [
        {"hex": "A1B2C3", "flight": "SWA123  ", "lat": 33.7, "lon": -112.3, "alt_baro": 35000, "alt_geom": 35450,
         "gs": 450.0, "track": 90.0, "geom_rate": -640, "baro_rate": -600, "seen_pos": 0.5},
        {"hex": "abc001", "lat": 33.5, "lon": -112.5, "alt_baro": 12000, "gs": 250, "track": 180, "seen_pos": 2.0},
        {"hex": "abc002", "lat": 33.4, "lon": -112.0, "alt_baro": "ground", "gs": 10, "track": 0},
        {"hex": "abc003", "alt_baro": 30000, "gs": 400},  # no position
        "garbage",
    ],
}


def test_parse_readsb_aircraft_json():
    acs = {a.hex: a for a in parse_aircraft_json(READSB)}
    assert set(acs) == {"a1b2c3", "abc001"}
    a = acs["a1b2c3"]
    assert a.flight == "SWA123" and a.alt_geometric
    assert a.alt_m == pytest.approx(35450 * FT_TO_M)  # alt_geom is WGS84 ellipsoid height, preferred
    assert a.gs_mps == pytest.approx(450 * KT_TO_MPS)
    assert a.vrate_mps == pytest.approx(-640 * 0.3048 / 60)
    assert a.t_pos_ns == pytest.approx(T0_NS - 0.5e9, abs=1e3)  # now - seen_pos
    b = acs["abc001"]
    assert not b.alt_geometric and b.alt_m == pytest.approx(12000 * FT_TO_M)
    assert b.t_pos_ns == pytest.approx(T0_NS - 2.0e9, abs=1e3)


def test_parse_adsbx_v2_shape_and_missing_now():
    v2 = {"ac": [{"hex": "def456", "lat": 1.0, "lon": 2.0, "alt_geom": 1000, "gs": 100, "track": 45,
                  "seen_pos": 1.0}], "now": T0_NS // 1_000_000, "total": 1}
    (a,) = parse_aircraft_json(v2)
    assert a.t_pos_ns == pytest.approx(T0_NS - 1e9, abs=1e6)  # 'now' in milliseconds
    (b,) = parse_aircraft_json({"ac": v2["ac"]}, received_ns=T0_NS)  # some endpoints omit 'now'
    assert b.t_pos_ns == pytest.approx(T0_NS - 1e9, abs=1e3)


def test_dead_reckoning_along_track():
    a = Aircraft("x", "", 33.0, -112.0, 10000.0, True, 200.0, 0.0, 5.0, T0_NS)
    lat, lon, alt = a.position_at(T0_NS + int(10e9))
    assert haversine_m(33.0, -112.0, lat, lon) == pytest.approx(2000.0, rel=1e-6)
    assert lat > 33.0 and lon == pytest.approx(-112.0)
    assert alt == pytest.approx(10050.0)
    lat2, lon2 = destination(0.0, 179.9, 90.0, 50_000.0)  # wraps across the antimeridian
    assert -180 <= lon2 <= -179.0 and lat2 == pytest.approx(0.0, abs=1e-9)


def test_azel_matches_shared_geodesy():
    a = Aircraft("x", "", 33.75, -112.30, 9000.0, True, 230.0, 270.0, 0.0, T0_NS)
    az, el, rng = a.azel_from(*OBSERVER, T0_NS)
    assert (az, el, rng) == pytest.approx(look_angles(*OBSERVER, 33.75, -112.30, 9000.0))
    later = a.azel_from(*OBSERVER, T0_NS + int(5e9))
    assert later[0] != pytest.approx(az)  # it moved west over 5 s


def test_feed_from_file_filters_stale_positions(tmp_path):
    p = tmp_path / "aircraft.json"
    p.write_text(json.dumps(READSB))
    cfg = parse_config({"adsb": {"source": "dump1090", "file_path": str(p), "max_position_age_s": 10}}).adsb
    feed = AdsbFeed(cfg, position=lambda: OBSERVER, clock=lambda: T0_NS)
    feed.poll_once()
    assert {a.hex for a in feed.aircraft(T0_NS)} == {"a1b2c3", "abc001"}
    assert {a.hex for a in feed.aircraft(T0_NS + int(9e9))} == {"a1b2c3"}  # abc001's position is 11 s old
    p.write_text("{not json")
    feed.poll_once()
    assert feed.errors == 1 and len(feed.aircraft(T0_NS)) == 2  # keeps the last good list


def test_adsbx_v2_http_api_with_key_header():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["path"], seen["key"] = self.path, self.headers.get("api-auth")
            body = json.dumps({"ac": READSB["aircraft"][:1], "now": T0_NS // 1_000_000}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    for port in HTTP_PORTS:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        cfg = parse_config({"adsb": {"source": "adsbx_v2", "api_key": "secret", "radius_nm": 25,
                                     "url": f"http://127.0.0.1:{port}/v2/lat/{{lat}}/lon/{{lon}}/dist/{{dist}}/"}}).adsb
        feed = AdsbFeed(cfg, position=lambda: OBSERVER, clock=lambda: T0_NS)
        feed.poll_once()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen["path"] == "/v2/lat/33.6292/lon/-112.3679/dist/25/"
    assert seen["key"] == "secret"
    assert [a.hex for a in feed.aircraft(T0_NS)] == ["a1b2c3"]


def test_feed_disabled_by_default():
    feed = AdsbFeed(parse_config({}).adsb, position=lambda: None)
    assert not feed.enabled
    feed.start()  # no thread, no error
    feed.stop()
    assert feed.aircraft(T0_NS) == []
