"""Volunteer sign-up endpoints with a real PostGIS database."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from skynet_server.app import create_app
from skynet_server.persistence import migrate

from .conftest import make_settings

ADMIN = {"Authorization": "Bearer admin-secret"}
VALID = {"email": "Jane@Example.org", "name": "Jane", "country": "us", "region": "Arizona", "city": "Surprise",
         "postal_code": "85374", "approx_lat": 33.62789, "approx_lon": -112.36512, "site_type": "rooftop",
         "has_hardware": "none", "interests": ["host_node", "software"], "notes": "Clear view to the south-west.",
         "consent": True, "website": ""}


@pytest.fixture
def client(database_url) -> Iterator[TestClient]:
    asyncio.run(migrate(database_url))
    settings = make_settings(database_url=database_url, volunteer_rate_per_hour=1000)
    with TestClient(create_app(settings)) as c:
        yield c


def test_signup_stores_rounded_record_and_upserts(client: TestClient):
    assert client.get("/api/v1/health").json()["db"] == "ok"
    resp = client.post("/api/v1/volunteers", json=VALID)
    assert resp.status_code == 201 and resp.json() == {"status": "received"}
    again = client.post("/api/v1/volunteers", json={**VALID, "email": "jane@example.org", "city": "Peoria"})
    assert again.status_code == 201 and again.json() == {"status": "received"}
    rows = client.get("/api/v1/volunteers", headers=ADMIN).json()["volunteers"]
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "jane@example.org" and row["country"] == "US" and row["city"] == "Peoria"
    assert (row["approx_lat"], row["approx_lon"]) == (33.63, -112.37)
    assert row["interests"] == ["host_node", "software"] and row["consent"] is True
    assert {"id", "created_at", "updated_at"} <= set(row) and "website" not in row


@pytest.mark.parametrize("change", [
    {"email": "not-an-email"}, {"email": None}, {"country": "ZZ"}, {"country": None}, {"consent": False},
    {"consent": "yes"}, {"name": "x" * 101}, {"notes": "x" * 1001}, {"postal_code": "1" * 21},
    {"approx_lat": 91}, {"approx_lon": -181}, {"site_type": "boat"}, {"has_hardware": "lots"},
    {"interests": ["host_node", "hacking"]},
])
def test_validation_errors(client: TestClient, change):
    body = {k: v for k, v in {**VALID, **change}.items() if v is not None}
    resp = client.post("/api/v1/volunteers", json=body)
    assert resp.status_code == 422 and "detail" in resp.json()


def test_malformed_and_oversized_bodies(client: TestClient):
    assert client.post("/api/v1/volunteers", content=b"{nope", headers={"Content-Type": "application/json"}
                       ).status_code == 422
    big = {**VALID, "notes": "x" * 5000}
    assert client.post("/api/v1/volunteers", json=big).status_code == 413


def test_honeypot_is_accepted_but_discarded(client: TestClient):
    resp = client.post("/api/v1/volunteers", json={**VALID, "email": "bot@spam.example", "website": "http://spam"})
    assert resp.status_code == 201 and resp.json() == {"status": "received"}
    # Even an otherwise invalid bot submission gets the same answer.
    assert client.post("/api/v1/volunteers", json={"website": "x"}).status_code == 201
    assert client.get("/api/v1/volunteers", headers=ADMIN).json() == {"volunteers": []}


def test_summary_hides_small_cells(client: TestClient):
    for i in range(3):
        client.post("/api/v1/volunteers", json={**VALID, "email": f"a{i}@example.org"})
    client.post("/api/v1/volunteers", json={**VALID, "email": "far@example.org", "approx_lat": 47.6,
                                            "approx_lon": -122.3})
    client.post("/api/v1/volunteers", json={**VALID, "email": "nowhere@example.org", "approx_lat": None,
                                            "approx_lon": None})
    assert client.get("/api/v1/volunteers/summary").json() == {
        "total": 5, "cells": [{"lat": 33.5, "lon": -112.5, "count": 3}]}


def test_admin_list_csv_and_delete(client: TestClient):
    client.post("/api/v1/volunteers", json={**VALID, "notes": "=HYPERLINK(\"http://x\")"})
    assert client.get("/api/v1/volunteers").status_code == 401
    resp = client.get("/api/v1/volunteers", params={"format": "csv"}, headers=ADMIN)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1 and rows[0]["email"] == "jane@example.org"
    assert rows[0]["interests"] == "host_node;software"
    assert rows[0]["notes"].startswith("'=")  # neutralised spreadsheet formula
    vid = rows[0]["id"]
    assert client.delete(f"/api/v1/volunteers/{vid}").status_code == 401
    assert client.delete(f"/api/v1/volunteers/{vid}", headers=ADMIN).status_code == 204
    assert client.delete(f"/api/v1/volunteers/{vid}", headers=ADMIN).status_code == 404
    assert client.get("/api/v1/volunteers", headers=ADMIN).json() == {"volunteers": []}


def test_rate_limit_per_client_ip(database_url):
    asyncio.run(migrate(database_url))
    settings = make_settings(database_url=database_url, trust_proxy=True)
    with TestClient(create_app(settings)) as c:
        def post(ip: str, i: int) -> int:
            body = {**VALID, "email": f"r{ip}-{i}@example.org"}
            return c.post("/api/v1/volunteers", json=body, headers={"X-Forwarded-For": f"203.0.113.9, {ip}"}
                          ).status_code
        assert [post("198.51.100.1", i) for i in range(6)] == [201] * 5 + [429]
        assert post("198.51.100.2", 0) == 201  # another client is unaffected
