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


def test_your_screenshot_case(app, monkeypatch):
    """'it is Currys in Farnborough' as the start, 'Aldershot Tesco' as the shop."""
    from loops import maps
    asked = []

    def search(q, near=None, bounded=False):
        asked.append(q)
        return {"Currys, Farnborough": [(51.29, -0.76, "Currys, Farnborough")],
                "Tesco, Aldershot": [(51.25, -0.76, "Tesco Extra, Aldershot, Hampshire")]}.get(q, [])
    monkeypatch.setattr(maps, "_search", search)

    class R:
        def raise_for_status(self): pass
        def json(self): return {"routes": [{"duration": 900, "distance": 5000}]}
    monkeypatch.setattr(maps.requests, "get", lambda url, **kw: R())
    i = app.post("/api/capture", json={"text": "Buy groceries at Aldershot Tesco tomorrow at 10am"}).json()["id"]
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["field"] == "here" and q["question"] == "Where are you right now?"
    assert q["extra"][0]["value"] == "__locate"
    app.patch(f"/api/loops/{i}", json={"here": "it is Currys in Farnborough"})
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["kind"] == "place" and "Tesco Extra, Aldershot" in q["question"]
    assert "Currys, Farnborough" in asked and "Tesco, Aldershot" in asked


def test_start_not_found_asks_where_you_are_again(app, monkeypatch):
    from loops import maps
    monkeypatch.setattr(maps, "_search", lambda q, near=None, bounded=False: [])
    i = app.post("/api/capture", json={"text": "Buy groceries at Tesco tomorrow at 10am"}).json()["id"]
    app.patch(f"/api/loops/{i}", json={"here": "somewhere odd"})
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["field"] == "here" and q["question"].startswith('I couldn\'t find "somewhere odd" on the map.')


def test_shop_not_found_asks_the_shop_again_or_skips_the_map(app, monkeypatch):
    from loops import maps
    monkeypatch.setattr(maps, "_search", lambda q, near=None, bounded=False: [(51.3, -0.75, "x")] if "Farnborough" in q else [])
    i = app.post("/api/capture", json={"text": "Buy groceries at Nowhere Shop tomorrow at 10am"}).json()["id"]
    app.patch(f"/api/loops/{i}", json={"here": "Farnborough"})
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["field"] == "place" and q["extra"][0]["value"] == "__skipmap"
    app.patch(f"/api/loops/{i}", json={"place": "__skipmap"})
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["field"] == "travel_mode"


def _fake_map(monkeypatch, seen):
    from loops import maps

    def search(q, near=None, bounded=False):
        seen.setdefault("q", []).append(q)
        if q in ("Halfords, Farnborough", "Currys"):   # two branches: the far one first, like a real search
            return [(53.4, -2.2, "Far branch, Manchester, England"), (51.29, -0.755, "Halfords, Farnborough Gate, Farnborough")]
        return []
    monkeypatch.setattr(maps, "_search", search)
    speed = {"foot": 725, "bike": 300, "car": 190}

    class R:
        def __init__(self, url): self.url = url
        def raise_for_status(self): pass
        def json(self):
            prof = self.url.split("routed-")[1].split("/")[0]
            return {"routes": [{"duration": speed[prof], "distance": 950}]}
    monkeypatch.setattr(maps.requests, "get", lambda url, **kw: seen.setdefault("urls", []).append(url) or R(url))


def test_one_question_at_a_time_then_the_estimate(app, monkeypatch):
    from datetime import date, timedelta
    seen = {}
    _fake_map(monkeypatch, seen)
    r = app.post("/api/capture", json={"text": "I need to buy skating shoes from Halfords in Farnborough"}).json()
    i = r["id"]
    nxt = lambda: app.get(f"/api/loops/{i}/next").json()
    assert loop(app, i)["place"] == "Halfords in Farnborough"       # taken from the note, not asked

    q = nxt(); assert q["kind"] == "calendar"
    day = (date.today() + timedelta(days=2)).isoformat()
    app.patch(f"/api/loops/{i}", json={"errand_day": day})
    q = nxt(); assert q["field"] == "start_at" and q["options"][0]["label"] == "08:00"
    ten = next(o for o in q["options"] if o["label"] == "10:00")
    app.patch(f"/api/loops/{i}", json={"start_at": ten["value"]})

    q = nxt(); assert q["field"] == "here"                             # "Where are you right now?"
    app.patch(f"/api/loops/{i}", json={"here": "51.30,-0.75"})
    q = nxt()
    assert q["kind"] == "place" and "Farnborough Gate" in q["question"] and q["map"] == {"lat": 51.29, "lon": -0.755}
    subs = {o["value"]: o.get("sub") for o in q["options"]}
    assert subs["walk"] == "13 min each way" and subs["cycle"] == "5 min each way" and subs["drive"] == "4 min each way"
    app.patch(f"/api/loops/{i}", json={"travel_mode": "cycle", "place_ok": True})

    q = nxt(); assert q["field"] == "there_min" and "Halfords" in q["question"]
    app.patch(f"/api/loops/{i}", json={"there_min": 30})
    q = nxt(); assert q["kind"] == "summary"
    assert q["lines"][0].startswith("Leave at 10:00") and "back about 10:40" in q["lines"][0]
    assert q["lines"][1] == "5 min cycling each way + 30 min there = 40 min."
    assert round(loop(app, i)["effort_h"] * 60) == 40


def test_busy_times_are_not_offered(app, monkeypatch):
    from datetime import date, datetime, timedelta
    from loops import planner
    day = date.today() + timedelta(days=3)
    lec = datetime.combine(day, datetime.min.time()).astimezone()
    monkeypatch.setattr(planner, "calendar_events", lambda a, b: [
        {"title": "Lecture", "start": lec.replace(hour=9).isoformat(), "end": lec.replace(hour=12).isoformat()}])
    i = app.post("/api/capture", json={"text": "Buy skating shoes"}).json()["id"]
    cal = app.get(f"/api/loops/{i}/next").json()
    assert next(o for o in cal["options"] if o["value"] == day.isoformat())["sub"] == "1 event"
    app.patch(f"/api/loops/{i}", json={"errand_day": day.isoformat()})
    labels = [o["label"] for o in app.get(f"/api/loops/{i}/next").json()["options"]]
    assert "08:00" in labels and "09:00" not in labels and "11:00" not in labels and "12:00" in labels
    assert labels[-1] == "Another day"


def test_nearest_branch_and_wrong_place(app, monkeypatch):
    seen = {}
    _fake_map(monkeypatch, seen)
    i = app.post("/api/capture", json={"text": "Buy a charger from Curry's Vonbra tomorrow at 3pm"}).json()["id"]
    app.patch(f"/api/loops/{i}", json={"here": "51.30,-0.75"})
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["kind"] == "place" and "Farnborough Gate" in q["question"]     # the closer of the two matches
    app.patch(f"/api/loops/{i}", json={"travel_mode": "__wrong"})
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["field"] == "place"


def test_uses_the_macs_location_without_asking(app, monkeypatch):
    from loops import maps
    monkeypatch.setattr(maps, "mac_location", lambda: "51.30000,-0.75000")
    monkeypatch.setattr(maps, "_search", lambda q, near=None, bounded=False: [(51.29, -0.755, "Halfords, Farnborough")])

    class R:
        def raise_for_status(self): pass
        def json(self): return {"routes": [{"duration": 600, "distance": 2000}]}
    monkeypatch.setattr(maps.requests, "get", lambda url, **kw: R())
    i = app.post("/api/capture", json={"text": "Buy skating shoes from Halfords in Farnborough tomorrow at 2pm"}).json()["id"]
    q = app.get(f"/api/loops/{i}/next").json()
    assert q["kind"] == "place" and "Halfords" in q["question"]      # never asked where you are
