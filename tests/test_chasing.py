"""Chasing keeps coming back: tasks that aren't urgent yet, or have no deadline, still get brought up again."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

NOW = datetime.now(timezone.utc)


def test_gentle_reminders_repeat_until_done(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from loops import agent, caller
    monkeypatch.setattr(caller, "_quiet", lambda st, now: False)
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)
    far = c.post("/api/capture", json={"text": "Apply for the Monzo role"}).json()["id"]
    c.patch(f"/api/loops/{far}", json={"due": (NOW + timedelta(days=4)).isoformat()})
    c.post("/api/capture", json={"text": "Reply to Mahan about coffee"})

    from loops import store

    def tick(at):
        monkeypatch.setattr(agent, "utcnow", lambda: at)
        monkeypatch.setattr(store, "now_iso", lambda: at.isoformat())
        return c.post("/api/nudges/tick").json()["nudges"]

    assert tick(NOW) == []                                   # just added: leave it alone
    n1 = tick(NOW + timedelta(minutes=40))
    assert len(n1) == 1 and n1[0]["id"] != far and "Coming up" in n1[0]["text"]   # soonest deadline first
    assert tick(NOW + timedelta(minutes=70)) == []           # not more than once an hour
    n2 = tick(NOW + timedelta(minutes=110))
    assert len(n2) == 1 and n2[0]["id"] == far and "It takes about" in n2[0]["text"]
    n3 = tick(NOW + timedelta(hours=4))
    assert n3 and n3[0]["id"] == n1[0]["id"]                 # and it comes back again
    c.post(f"/api/loops/{far}/reply", json={"text": "done"})


def test_past_deadline_is_asked_again_a_few_times(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from loops import agent, caller
    monkeypatch.setattr(caller, "_quiet", lambda st, now: False)
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)
    i = c.post("/api/capture", json={"text": "Send the council form"}).json()["id"]
    c.patch(f"/api/loops/{i}", json={"due": (NOW - timedelta(minutes=5)).isoformat()})

    from loops import store

    def asked(at):
        monkeypatch.setattr(agent, "utcnow", lambda: at)
        monkeypatch.setattr(store, "now_iso", lambda: at.isoformat())
        return [n for n in c.post("/api/nudges/tick").json()["nudges"] if n["id"] == i]

    got = [bool(asked(NOW + timedelta(hours=h))) for h in (0, 0.5, 3.1, 6.2, 9.3, 12.4)]
    assert got[0] and not got[1] and sum(got) == 3


def test_other_answer_when_ticking_off(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)
    # a chase task: you chased, no reply yet, so it stays open and comes back in 3 days
    k = c.post("/api/capture", json={"text": "Chase Priya about the referral"}).json()["id"]
    r = c.post(f"/api/loops/{k}/finish", json={"happened": "other", "other": "Chased her, no reply yet"}).json()
    assert r["kept"] and "check again" in r["message"]
    assert k in [l["id"] for l in c.get("/api/state").json()["loops"] if l["type"] == "waiting"]
    # a task of yours: done, and now you're waiting on her
    i = c.post("/api/capture", json={"text": "Email Priya my CV for the referral"}).json()["id"]
    c.patch(f"/api/loops/{i}", json={"person": "Priya"})
    r = c.post(f"/api/loops/{i}/finish", json={"happened": "other", "other": "I chased her up but she hasn't replied yet"}).json()
    loops = c.get("/api/state").json()["loops"]
    assert i not in [l["id"] for l in loops]                       # my part is done
    w = [l for l in loops if l["type"] == "waiting" and l["id"] != k]
    assert r["created"] and w and w[0]["summary"] == "Hear back from Priya" and "hasn't replied" in w[0]["note"]
    # not done yet in your own words: it stays on the list with the note
    j = c.post("/api/capture", json={"text": "Send the council form"}).json()["id"]
    r = c.post(f"/api/loops/{j}/finish", json={"happened": "other", "other": "Couldn't find the form, will do it tomorrow"}).json()
    assert r["kept"] and j in [l["id"] for l in c.get("/api/state").json()["loops"]]
