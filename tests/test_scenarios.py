"""20 edge-case scenarios, run end to end through the API with no API key and no accounts.
Each one is a thing a real user would do or receive. Run: python -m pytest -q tests/test_scenarios.py -v"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.models import Message

NOW = datetime.now(timezone.utc)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app, db
    return TestClient(app), db


def feed(db, *msgs):
    from loops.decisions import RuleDecisions
    from loops.engine import sync

    class F:
        name = "gmail"
        def fetch(self, since): return list(msgs)
    return sync(db(), [F()], RuleDecisions())


def mail(mid, sender, name, subject, text, when=None, me=False, thread=None):
    return Message("gmail", mid, thread or mid, sender, name, me, text, when or NOW - timedelta(minutes=30), subject)


def state(c):
    return c.get("/api/state").json()


def open_tasks(c):
    return [l for l in state(c)["loops"] if l["status"] == "open"]


# 1
def test_01_errand_asks_the_day_first_on_a_calendar(app):
    c, _ = app
    r = c.post("/api/capture", json={"text": "Buy skating shoes"}).json()
    assert r["errand"] and open_tasks(c)[0]["summary"] == "Buy skating shoes"
    q = c.get(f"/api/loops/{r['id']}/next").json()
    assert q["kind"] == "calendar" and q["field"] == "errand_day"      # no day given: don't assume one
    assert len(q["options"]) >= 13


# 2
def test_02_errand_answers_set_the_time_needed_and_start(app):
    c, _ = app
    i = c.post("/api/capture", json={"text": "Buy groceries tomorrow"}).json()["id"]
    start = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    for f, v in [("start_at", start.isoformat()), ("travel_mode", "cycle"), ("travel_min", 10), ("place", "Tesco on the high street")]:
        assert c.patch(f"/api/loops/{i}", json={f: v}).status_code == 200
    l = open_tasks(c)[0]
    assert round(l["effort_h"] * 60) == 50                           # 30 min shopping + 10 min each way
    assert datetime.fromisoformat(l["commit_at"]) == start.astimezone(timezone.utc)
    assert "Tesco" in (l["note"] or "") and "cycling" in (l["note"] or "")
    assert c.post("/api/nudges/tick").json()["nudges"] == []         # no nagging before 10am


# 3
def test_03_same_task_twice_is_one_task(app):
    c, _ = app
    c.post("/api/capture", json={"text": "Buy groceries tomorrow"})
    c.post("/api/capture", json={"text": "buy groceries tomorrow."})
    assert len(open_tasks(c)) == 1


# 4
def test_04_old_duplicates_are_merged(app):
    c, db = app
    s = db()
    for _ in range(2):
        s.create_loop(source="manual", thread_id="", type="task", person="", summary="Buy groceries tomorrow", done_when="",
                      due=(NOW + timedelta(days=1)).isoformat(), cost=30, consequence="minor", reversible=1, hard_deadline=0,
                      effort_h=0.5, stakes="")
    assert len(open_tasks(c)) == 1


# 5
def test_05_updated_invite_from_unknown_sender_is_parsed(app):
    c, db = app
    when = NOW + timedelta(days=3)
    subj = f"Updated invitation from an unknown sender: Session 2 - Build — Workshop @ {when:%a %b %d, %Y} 10am - 12pm (BST) (me@gmail.com)"
    feed(db, mail("u1", "hello@gdg.london", "Hello GDG London", subj, "Join with Google Meet https://meet.google.com/x"))
    l = open_tasks(c)[0]
    assert l["summary"] == "Meeting with Hello GDG London: Session 2 - Build — Workshop", l["summary"]
    assert datetime.fromisoformat(l["due"]).astimezone(timezone(timedelta(hours=1))).hour == 10


# 6
def test_06_invite_for_a_meeting_that_already_happened_is_ignored(app):
    c, db = app
    past = NOW - timedelta(days=2)
    subj = f"Invitation: Coffee chat @ {past:%a %d %b %Y} 3pm - 3:30pm (GMT) (me@gmail.com)"
    feed(db, mail("p1", "sam@x.com", "Sam", subj, "Join with Google Meet https://meet.google.com/x", when=past - timedelta(days=1)))
    assert open_tasks(c) == []


# 7
def test_07_meeting_that_has_ended_drops_off_quietly(app):
    c, db = app
    s = db()
    s.create_loop(source="gmail", thread_id="m", type="meeting", person="Hello GDG London", summary="Meeting with Hello GDG London",
                  done_when="", due=(NOW - timedelta(hours=5)).isoformat(), cost=55, consequence="relationship", reversible=0,
                  hard_deadline=1, effort_h=0.17, stakes="")
    assert open_tasks(c) == [] and c.post("/api/nudges/tick").json()["nudges"] == []


# 8
def test_08_long_overdue_task_is_not_the_top_and_asked_only_once(app):
    c, db = app
    old = c.post("/api/capture", json={"text": "Send the council form"}).json()["id"]
    c.patch(f"/api/loops/{old}", json={"due": (NOW - timedelta(days=2)).isoformat()})
    new = c.post("/api/capture", json={"text": "Email Priya the slides by tomorrow"}).json()["id"]
    st = state(c)
    active = [l for l in st["loops"] if l["status"] == "open" and not l.get("past_deadline")]
    assert active and active[0]["id"] == new
    assert any(l["id"] == old and l.get("past_deadline") for l in st["loops"])
    n1 = [n for n in c.post("/api/nudges/tick").json()["nudges"] if n["id"] == old]
    from loops import agent
    agent.utcnow = lambda: NOW + timedelta(minutes=10)
    try:
        n2 = [n for n in c.post("/api/nudges/tick").json()["nudges"] if n["id"] == old]
    finally:
        agent.utcnow = lambda: datetime.now(timezone.utc)
    assert len(n1) <= 1 and n2 == []


# 9
def test_09_newsletter_does_not_become_a_task(app):
    c, db = app
    feed(db, mail("n1", "no-reply@medium.com", "Medium Daily Digest", "Stories for you",
                  "Here are today's top stories. Unsubscribe here."))
    assert open_tasks(c) == []


# 10
def test_10_job_alert_becomes_a_lead_not_a_reply(app):
    c, db = app
    feed(db, mail("j1", "jobalerts-noreply@linkedin.com", "LinkedIn Job Alerts", "Data analyst: 1 new job",
                  "Graduate Data Analyst\nMonzo\nLondon\nView job: https://www.linkedin.com/jobs/view/123/\n"))
    assert open_tasks(c) == [] and [x["title"] for x in state(c)["leads"]] == ["Graduate Data Analyst"]


# 11
def test_11_scam_recruiter_is_neither_a_lead_nor_a_task(app):
    c, db = app
    feed(db, mail("s1", "hr.jobs@gmail.com", "Recruiter", "Remote role",
                  "I came across your profile and have a role that would be a great fit. Small training fee. Message me on WhatsApp?"))
    assert open_tasks(c) == [] and state(c)["leads"] == []


# 12
def test_12_confirmation_from_another_company_does_not_close_the_task(app):
    c, db = app
    i = c.post("/api/capture", json={"text": "Apply to Monzo"}).json()["id"]
    feed(db, mail("a1", "careers@acme.com", "Acme Careers", "Thank you for applying", "We have received your application."))
    c.post("/api/nudges/tick")
    from loops.evidence import sweep
    sweep(db())
    assert any(l["id"] == i for l in open_tasks(c))


# 13
def test_13_confirmation_closes_then_not_done_yet_reopens_the_rest(app):
    c, db = app
    i = c.post("/api/capture", json={"text": "Apply to Monzo"}).json()["id"]
    feed(db, mail("a2", "no-reply@monzo.com", "Monzo Careers", "Thanks for applying to Monzo", "We've received your application."))
    assert state(c)["celebrate"][0]["id"] == i
    due = c.get("/api/days-options").json()[1]["value"]
    c.post(f"/api/loops/{i}/not-done", json={"left": "I still need to record the video interview", "due": due})
    assert open_tasks(c)[0]["summary"] == "Record the video interview"


# 14
def test_14_assessment_on_behalf_of_employer_is_one_task(app):
    c, db = app
    feed(db, mail("h1", "noreply@shl.com", "SHL on behalf of Shell", "Shell graduate programme: online assessment",
                  "You have been invited to complete the Shell numerical reasoning assessment. Please complete it by Friday."))
    t = open_tasks(c)
    assert len(t) == 1 and t[0]["summary"] == "Complete the Shell numerical reasoning", t


# 15
def test_15_very_long_text_is_kept_short(app):
    c, _ = app
    r = c.post("/api/capture", json={"text": "Write up " + "the quarterly report and " * 40})
    assert r.status_code == 200 and len(open_tasks(c)[0]["summary"]) <= 120


# 16
def test_16_emoji_and_non_english_text(app):
    c, _ = app
    c.post("/api/capture", json={"text": "Call Priya \U0001F4DE about the offer, café at 3pm"})
    l = open_tasks(c)[0]
    assert "\U0001F4DE" in l["summary"] and l["type"] == "call" and l["person"] == "Priya"


# 17
def test_17_empty_and_nonsense_times(app):
    c, _ = app
    assert c.post("/api/capture", json={"text": "   "}).status_code == 400
    assert c.post("/api/capture", json={"text": "Pay rent tomorrow at 25:00"}).status_code == 200
    assert open_tasks(c)[0]["summary"] == "Pay rent"


# 18
def test_18_in_10_minutes(app):
    c, _ = app
    c.post("/api/capture", json={"text": "Remind me to call Sam in 10 minutes"})
    l = open_tasks(c)[0]
    mins = (datetime.fromisoformat(l["due"]) - datetime.now(timezone.utc)).total_seconds() / 60
    assert 8 <= mins <= 11 and l["type"] == "call" and l["person"] == "Sam" and l["summary"] == "Call Sam"


# 19
def test_19_reply_later_then_done(app):
    c, _ = app
    i = c.post("/api/capture", json={"text": "Prepare for my interview, it takes an hour"}).json()["id"]
    c.patch(f"/api/loops/{i}", json={"due": (NOW + timedelta(minutes=65)).isoformat()})
    assert c.post("/api/nudges/tick").json()["nudges"]
    c.post(f"/api/loops/{i}/reply", json={"text": "in 10 minutes"})
    assert c.post("/api/nudges/tick").json()["nudges"] == []
    assert c.post(f"/api/loops/{i}/reply", json={"text": "done"}).json()["action"] == "close"


# 20
def test_20_focus_session_on_one_task_pauses_reminders(app):
    c, _ = app
    i = c.post("/api/capture", json={"text": "Prepare for my interview, it takes an hour"}).json()["id"]
    c.patch(f"/api/loops/{i}", json={"due": (NOW + timedelta(minutes=65)).isoformat()})
    c.post("/api/session/start", json={"answers": {"kind": "custom", "which": "write the follow-up email", "minutes": "30", "rhythm": "25/5"}})
    assert c.get("/api/session").json()["active"]["title"] == "Write the follow-up email"
    assert c.post("/api/nudges/tick").json()["nudges"] == []
    c.post("/api/session/end")
    assert c.post("/api/nudges/tick").json()["nudges"]


def test_delete_and_undo(app):
    c, _ = app
    i = c.post("/api/capture", json={"text": "Call the dentist"}).json()["id"]
    assert c.delete(f"/api/loops/{i}").status_code == 200
    assert not open_tasks(c) and all(l["id"] != i for l in state(c)["closed"])     # gone everywhere
    c.post(f"/api/loops/{i}/restore")
    assert [l["id"] for l in open_tasks(c)] == [i]


def test_no_made_up_reason_for_your_own_notes(app):
    c, _ = app
    i = c.post("/api/capture", json={"text": "Buy groceries"}).json()["id"]
    l = next(l for l in open_tasks(c) if l["id"] == i)
    assert l["stakes"] == "" and l["forecast"]["if_missed"] == ""                   # the card asks you instead
    c.patch(f"/api/loops/{i}", json={"stakes": "Nothing in for breakfast"})
    l = next(l for l in open_tasks(c) if l["id"] == i)
    assert l["forecast"]["if_missed"] == "Nothing in for breakfast"
