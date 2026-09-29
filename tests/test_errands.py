"""Errands: what the note says is used, travel time is looked up, and times read as minutes."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def app(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "e.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app
    return TestClient(app)


def loop(c, i):
    return next(l for l in c.get("/api/state").json()["loops"] if l["id"] == i)


def test_durations_read_like_people_say_them():
    from loops.forecast import dur
    assert dur(55 / 60) == "55 min" and dur(70 / 60) == "1 h 10 min" and dur(2) == "2 h" and dur(1.7) == "1 h 42 min"


def test_walk_20_each_way_and_15_shopping_is_55_minutes(app):
    r = app.post("/api/capture", json={"text": "Buy groceries, walking 20 minutes each way and 15 minutes for shopping"}).json()
    fields = [q["field"] for q in r["questions"]]
    assert "travel_mode" not in fields and "there_min" not in fields and "home" not in fields
    l = loop(app, r["id"])
    assert round(l["effort_h"] * 60) == 55
    assert "20 min each way + 15 min there = 55 min" in l["note"]


def test_travel_time_is_looked_up_not_asked(app, monkeypatch):
    from loops import maps
    calls = []

    def fake_osm(origin, dest, mode):
        calls.append((origin, dest, mode))
        return 17 * 60 + 20, 1400          # 17 min 20 s -> rounded up to 18
    monkeypatch.setattr(maps, "_osm", fake_osm)
    r = app.post("/api/capture", json={"text": "Buy groceries tomorrow"}).json()
    fields = [q["field"] for q in r["questions"]]
    assert fields.index("home") < fields.index("place") < fields.index("travel_min")
    i = r["id"]
    app.patch(f"/api/loops/{i}", json={"travel_mode": "walk"})
    app.patch(f"/api/loops/{i}", json={"home": "SW1A 1AA"})
    p = app.patch(f"/api/loops/{i}", json={"place": "Tesco on the high street"}).json()
    assert p["set"]["travel_min"] == 18 and p["route"]["via"] == "OpenStreetMap"
    app.patch(f"/api/loops/{i}", json={"there_min": 15})
    assert round(loop(app, i)["effort_h"] * 60) == 18 * 2 + 15
    assert calls == [("SW1A 1AA", "Tesco on the high street", "walk")]
    # home is remembered, so the next errand doesn't ask again
    r2 = app.post("/api/capture", json={"text": "Pick up parcel from the post office"}).json()
    assert "home" not in [q["field"] for q in r2["questions"]]


def test_falls_back_to_asking_when_lookup_fails(app, monkeypatch):
    from loops import maps
    monkeypatch.setattr(maps, "_osm", lambda *a: None)
    i = app.post("/api/capture", json={"text": "Buy groceries tomorrow"}).json()["id"]
    app.patch(f"/api/loops/{i}", json={"home": "SW1A 1AA"})
    app.patch(f"/api/loops/{i}", json={"travel_mode": "walk"})
    p = app.patch(f"/api/loops/{i}", json={"place": "Somewhere unknown"}).json()
    assert p["set"] == {} and p["route"] is None       # the UI then asks "how long one way?"
