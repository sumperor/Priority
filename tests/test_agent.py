"""Chasing: nudges fire at the right times and replies get numerically honest answers."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_chase_and_reply(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)
    now = datetime.now(timezone.utc)
    i = c.post("/api/capture", json={"text": "Prepare for my interview, it takes an hour"}).json()["id"]
    # Interview in 65 minutes: latest start (1h x 1.25 + 15m) has already passed, so it should chase
    c.patch(f"/api/loops/{i}", json={"due": (now + timedelta(minutes=65)).isoformat()})
    n = c.post("/api/nudges/tick").json()["nudges"]
    assert n and n[0]["id"] == i
    assert c.post("/api/nudges/tick").json()["nudges"] == []          # not again within 2 minutes

    r = c.post(f"/api/loops/{i}/reply", json={"text": "I'm going to start studying in 10 minutes"}).json()
    print(r["reply"])
    assert "5 min short" in r["reply"] and "interview" in r["reply"]  # 55 min left for 60 min of prep
    assert c.post("/api/nudges/tick").json()["nudges"] == []          # waits until the committed time

    print(c.post(f"/api/loops/{i}/reply", json={"text": "I'll start at 5pm"}).json()["reply"])
    r = c.post(f"/api/loops/{i}/reply", json={"text": "ok starting now"}).json()
    print(r["reply"])
    assert r["reply"].startswith("Good.")
    r = c.post(f"/api/loops/{i}/reply", json={"text": "done"}).json()
    assert r["action"] == "close"

    # Spoken correction
    ch = c.post(f"/api/loops/{i}/revise", json={"text": "No, I meant it takes 30 minutes"}).json()["changed"]
    assert "effort_h" in ch
