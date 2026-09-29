"""Check before chasing: confirmation emails close loops, 'not done yet' narrows them, leads and write-ups work.
No API key or accounts needed."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.models import Message

NOW = datetime.now(timezone.utc)


def msg(mid, sender, name, subject, text, mins_ago=1, me=False, thread=None):
    return Message("gmail", mid, thread or mid, sender, name, me, text, NOW - timedelta(minutes=mins_ago), subject)


def client(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "e.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app, db
    return TestClient(app), db


def test_ack_email_closes_before_chasing(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    i = c.post("/api/capture", json={"text": "Apply to Monzo, it takes an hour"}).json()["id"]
    assert c.get("/api/state").json()["loops"][0]["area"] == "Jobs"
    c.patch(f"/api/loops/{i}", json={"due": (NOW + timedelta(minutes=50)).isoformat()})

    # An unrelated email must not count; the Monzo confirmation must
    db().upsert_messages([
        msg("a", "jobs@acme.com", "Acme", "Thank you for applying", "We have received your application."),
        msg("b", "no-reply@monzo.com", "Monzo Careers", "Thanks for applying to Monzo",
            "Hi, thank you for your application for Graduate Analyst. We'll be in touch."),
    ])
    n = c.post("/api/nudges/tick").json()["nudges"]
    assert n and n[0]["kind"] == "done" and "Monzo" in n[0]["text"]
    s = c.get("/api/state").json()
    assert not any(l["id"] == i for l in s["loops"])
    card = s["celebrate"][0]
    assert card["id"] == i and card["evidence"]["from"] == "Monzo Careers"

    # "Not done yet": what's left becomes the task, with a new deadline, and the same email isn't reused
    due = c.get("/api/days-options").json()[2]["value"]
    c.post(f"/api/loops/{i}/not-done", json={"left": "record the video interview part", "due": due})
    s = c.get("/api/state").json()
    l = next(l for l in s["loops"] if l["id"] == i)
    assert l["summary"] == "Record the video interview part" and "Monzo" in l["note"] and not s["celebrate"]
    assert all(x.get("kind") != "done" for x in c.post("/api/nudges/tick").json()["nudges"])
    assert c.get("/api/state").json()["accuracy"]["close"]["n"] == 1   # labelled wrong, for calibration


def test_sync_without_key_and_leads(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    from loops.decisions import get_engine
    from loops.engine import sync

    class Fake:
        name = "fake"
        def __init__(self, m): self.m = m
        def fetch(self, since): return self.m

    alert = ("Graduate Data Analyst\nMonzo\nLondon\nView job: https://www.linkedin.com/jobs/view/123456/?ref=alert\n\n"
             "Crypto Trader, earn £5,000 per week\nQuickCash\nRemote\nhttps://bit.ly/xyz/jobs/1\n")
    scam = ("Hi, I came across your profile and have a role that would be a great fit. "
            "There is a small training fee, please message me on WhatsApp.")
    msgs = [msg("1", "jobalerts-noreply@linkedin.com", "LinkedIn Job Alerts", "Data analyst: 2 new jobs", alert),
            msg("2", "sam@x.com", "Sam", "Deck", "Can you send the deck by Friday?", mins_ago=30),
            msg("3", "recruit.jobs@gmail.com", "Recruiter", "Opportunity", scam)]
    s = db()
    new, _ = sync(s, [Fake(msgs)], get_engine())                   # rules engine: no key, no crash
    types = sorted(s.get_loop(x)["type"] for x in new)
    assert types == ["reply"], types                              # the alert became a lead, not a reply
    rows = s.db.execute("SELECT title, company, credible FROM leads ORDER BY id").fetchall()
    assert ("Graduate Data Analyst", "Monzo", "credible") == tuple(rows[0])
    assert all(r["credible"] == "suspicious" for r in rows[1:])
    shown = c.get("/api/state").json()["leads"]
    assert [x["title"] for x in shown] == ["Graduate Data Analyst"]
    i = c.post(f"/api/leads/{shown[0]['id']}/apply").json()["id"]
    l = s.get_loop(i)
    assert l["summary"] == "Apply to Graduate Data Analyst at Monzo" and l["area"] == "Jobs"
    assert c.get("/api/state").json()["leads"] == []


def test_interview_write_up(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    i = c.post("/api/capture", json={"text": "Deloitte interview tomorrow at 10"}).json()["id"]
    q = c.get(f"/api/loops/{i}/closing").json()
    assert any(f["use"] == "interview_notes" for f in q["followups"])
    notes = ("Met Priya from the analytics team. We talked about their move to real-time fraud models. "
             "She asked about my dissertation. They will get back to me by Friday.")
    r = c.post(f"/api/loops/{i}/finish", json={"happened": "yes", "answers": [
        {"question": "notes", "use": "interview_notes", "value": notes}]}).json()
    w = r["write_up"]
    assert w["title"] == "Deloitte interview" and "real-time fraud models" in w["thank_you"]
    assert any("Friday" in f for f in w["follow_ups"])
    assert any("thank-you" in x["summary"] for x in r["created"])


def test_account_status_and_custom_focus(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    from loops import config as C
    from loops import engine
    monkeypatch.setattr(C, "CONNECTORS", ["gmail", "slack"])
    monkeypatch.setattr(engine, "SOURCES", {})

    class Ok:
        name = "gmail"
        def fetch(self, since): return []

    class Broken:
        name = "slack"
        def fetch(self, since): raise RuntimeError("set SLACK_USER_TOKEN in .env")

    engine.sync(db(), [Ok(), Broken()], __import__("loops.decisions", fromlist=["x"]).RuleDecisions())
    src = {x["name"]: x for x in c.get("/api/state").json()["sources"]}
    assert src["gmail"]["state"] == "ok" and src["gmail"]["count"] == 0 and src["gmail"]["at"]
    assert src["slack"]["state"] == "error" and ".env" in src["slack"]["fix"]
    monkeypatch.setattr(C, "CONNECTORS", [])  # the session view reads calendars; keep it offline

    kinds = [o["value"] for o in c.get("/api/session/intake").json()["questions"][0]["options"]]
    assert kinds[-1] == "custom"
    c.post("/api/session/start", json={"answers": {"kind": "custom", "which": "write the follow-up email to Priya",
                                                   "minutes": "30", "rhythm": "25/5"}})
    assert c.get("/api/session").json()["active"]["title"] == "Write the follow-up email to Priya"
