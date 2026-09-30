"""One reading per email: Claude's decision (faked here), the rules fallback, corrections and the review screen."""
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
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app, db
    return TestClient(app), db


def run(db, *msgs):
    from loops.decisions import RuleDecisions
    from loops.engine import sync

    class F:
        name = "gmail"
        def fetch(self, since): return list(msgs)
    sync(db(), [F()], RuleDecisions())


def mail(mid, sender, name, subject, text, ago=timedelta(hours=1)):
    return Message("gmail", mid, mid, sender, name, False, text, NOW - ago, subject)


def test_claude_reading_becomes_the_task_with_its_own_question(app, monkeypatch):
    c, db = app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from loops import llm
    seen = {}

    def fake(system, user, max_tokens=700, model=""):
        seen["model"], seen["user"] = model, user
        return {"kind": "action", "task": "Change your Spotify plan to Student", "who": "Spotify", "due": "",
                "event_start": "", "event_end": "", "place": "", "link": "https://spotify.com/account/plan",
                "close_question": "Did you switch Spotify to the Student plan?",
                "followups": [{"question": "Did the price drop?", "kind": "choice", "options": ["Yes", "No"]}],
                "why": "Your student discount needs re-verifying."}
    monkeypatch.setattr(llm, "ask_json", fake)
    run(db, mail("s1", "no-reply@spotify.com", "Spotify", "Keep your discount",
                 "Re-verify to stay on Student.\nChange plan https://spotify.com/account/plan"))
    l = [x for x in c.get("/api/state").json()["loops"] if x["person"] == "Spotify"][0]
    assert l["summary"] == "Change your Spotify plan to Student" and l["link"] == "https://spotify.com/account/plan"
    assert seen["model"] == "claude-haiku-4-5"
    q = c.get(f"/api/loops/{l['id']}/closing").json()
    assert q["happened"] == "Did you switch Spotify to the Student plan?" and q["followups"][0]["question"] == "Did the price drop?"
    R = c.get("/api/review").json()
    assert R["items"][0]["by"] == "claude" and R["items"][0]["kind"] == "action"


def test_made_up_links_are_dropped(app, monkeypatch):
    c, db = app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from loops import llm
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"kind": "reply", "task": "Reply to Sam", "who": "Sam",
                                                         "link": "https://phish.example/login", "close_question": ""})
    run(db, mail("p1", "sam@x.com", "Sam", "Quick one", "Are you free to chat later?"))
    l = [x for x in c.get("/api/state").json()["loops"] if x["person"] == "Sam"][0]
    assert not l["link"]


def test_without_a_key_rules_decide_and_it_is_still_recorded(app):
    c, db = app
    run(db, mail("n1", "news@shop.com", "Shop", "Autumn sale", "Half price everything."))
    R = c.get("/api/review").json()
    assert R["items"][0]["kind"] == "fyi" and R["items"][0]["by"] == "rules" and R["accuracy"] is None


def test_corrections_fix_the_task_and_teach_it(app, monkeypatch):
    c, db = app
    run(db, mail("w1", "priya@x.com", "Priya", "Slides", "Can you send me the slides?"),
        mail("w2", "promo@brand.com", "Brand", "Tell us what you think", "Can you rate your order?"))
    loops = {x["person"]: x for x in c.get("/api/state").json()["loops"]}
    # wrong: the brand email isn't a task at all
    r = c.post(f"/api/loops/{loops['Brand']['id']}/wrong", json={"verdict": "wrong", "kind": "fyi"}).json()
    assert "won't become tasks" in r["message"]
    assert "Brand" not in {x["person"] for x in c.get("/api/state").json()["loops"]}
    # and the next email from them is ignored
    run(db, mail("w3", "promo@brand.com", "Brand", "One more thing", "Can you rate us?"))
    assert "Brand" not in {x["person"] for x in c.get("/api/state").json()["loops"]}
    # right: marked, accuracy counts it
    R = c.get("/api/review").json()
    priya = next(x for x in R["items"] if x["msg_id"] == "w1")
    c.post(f"/api/review/gmail/w1", json={"verdict": "right"})
    R = c.get("/api/review").json()
    assert R["marked"] == 2 and R["right"] == 1 and R["accuracy"] == 50
    # corrections are shown to Claude as examples
    from loops.reader import _examples
    assert "Tell us what you think" in _examples(db())


def test_calendar_attachment_and_quotes():
    from loops.connectors.gmail import clean_body, event_line, parse_ics
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Final interview - Monzo\r\nDTSTART:20301002T130000Z\r\n"
           "DTEND:20301002T140000Z\r\nLOCATION:Broadwalk House\r\nDESCRIPTION:Join https://monzo.zoom.us/j/99\r\n"
           " 887\r\nEND:VEVENT\r\nEND:VCALENDAR")
    ev = parse_ics(ics)
    assert ev["title"] == "Final interview - Monzo" and ev["start"] == datetime(2030, 10, 2, 13, tzinfo=timezone.utc)
    assert ev["join"].startswith("https://monzo.zoom.us/j/99")
    line = event_line(ev)
    assert line.startswith("Calendar event: Final interview - Monzo") and "Place: Broadwalk House" in line
    from loops.invites import when_loose
    s, e = when_loose(line, now=datetime(2030, 9, 30, tzinfo=timezone.utc))
    assert s == datetime(2030, 10, 2, 13, tzinfo=timezone.utc) or s.astimezone().hour == ev["start"].astimezone().hour
    body = "Sounds good, see you then.\n\nOn Mon, 29 Sep 2030 at 10:00, Sam <sam@x.com> wrote:\n> Can we meet?\n> Thanks"
    assert clean_body(body) == "Sounds good, see you then."
