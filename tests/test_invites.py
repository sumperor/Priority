"""Calendar invites become meetings at their real time; captured tasks are tidied and not duplicated."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.models import Message

GOOGLE_INVITE = """This event isn't in your calendar yet
You haven't interacted with mahan@vcpulse.io before. Do you want to automatically add this and future invitations from them to your calendar?
Add to calendar https://calendar.google.com/calendar/u/0/r/eventedit/abc
Meeting
Wednesday 1 Oct 2025 ⋅ 3pm – 3:30pm
United Kingdom Time
Join with Google Meet
https://meet.google.com/abc-defg-hij
Organiser
Mahan Agabegi
mahan@vcpulse.io
Guests
View all guest info https://calendar.google.com/x
Reply for sumedh@gmail.com
Invitation from Google Calendar
You are receiving this email because you are an attendee on the event."""


def test_parse_google_invite():
    from loops.invites import is_invite, parse, preview, summary
    m = Message("gmail", "i1", "i1", "mahan@vcpulse.io", "Mahan Agabegi", False, GOOGLE_INVITE, datetime.now(timezone.utc),
                "Invitation: Meeting @ Wed 1 Oct 2025 3pm - 3:30pm (BST) (sumedh@gmail.com)")
    assert is_invite(m)
    inv = parse(m)
    assert inv["start"] == datetime(2025, 10, 1, 14, 0, tzinfo=timezone.utc)
    assert inv["end"] - inv["start"] == timedelta(minutes=30)
    assert inv["organizer"] == "Mahan Agabegi" and inv["join"] == "Google Meet" and inv["generic"]
    assert summary(inv) == "Meeting with Mahan Agabegi, topic not given"
    p = preview(inv)
    assert "isn't in your calendar" not in p and "Google Meet" in p and "No agenda" in p


def client(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app, db
    return TestClient(app), db


def test_invite_becomes_a_meeting_not_a_reply(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    from loops.decisions import RuleDecisions
    from loops.engine import sync
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    subj = f"Invitation: Chat about VC Pulse @ {start:%a %d %b %Y %H:%M} - {(start + timedelta(hours=1)):%H:%M} (UTC) (me@gmail.com)"

    class F:
        name = "gmail"
        def fetch(self, since): return [Message("gmail", "i2", "i2", "mahan@vcpulse.io", "Mahan Agabegi", False,
                                                GOOGLE_INVITE, datetime.now(timezone.utc) - timedelta(hours=1), subj)]
    new, _ = sync(db(), [F()], RuleDecisions())
    loops = c.get("/api/state").json()["loops"]
    assert len(loops) == 1 and loops[0]["type"] == "meeting"
    l = loops[0]
    assert l["summary"] == "Meeting with Mahan Agabegi: Chat about VC Pulse"
    assert datetime.fromisoformat(l["due"]) == start and l["hard_deadline"] == 1
    assert "calendar yet" not in l["origin"]["preview"] and "Organised by Mahan Agabegi" in l["origin"]["preview"]


def test_capture_tidies_and_skips_duplicates(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    a = c.post("/api/capture", json={"text": "Buy groceries tomorrow"}).json()
    b = c.post("/api/capture", json={"text": "buy groceries tomorrow"}).json()
    assert b["duplicate"] and b["id"] == a["id"]
    l = c.get("/api/state").json()["loops"]
    assert len(l) == 1 and l[0]["summary"] == "Buy groceries" and l[0]["area"] == "Personal"
    from loops.server import tidy_summary
    assert tidy_summary("Call mum tonight") == "Call mum" and tidy_summary("Revise stats on Friday at 3pm") == "Revise stats"
    assert tidy_summary("Tomorrow") == "Tomorrow"


def test_old_reply_task_for_an_invite_is_replaced(tmp_path, monkeypatch):
    c, db = client(tmp_path, monkeypatch)
    from loops.decisions import RuleDecisions
    from loops.engine import detect
    s = db()
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    m = Message("gmail", "i3", "i3", "mahan@vcpulse.io", "Mahan Agabegi", False, GOOGLE_INVITE,
                datetime.now(timezone.utc) - timedelta(hours=1), f"Invitation: Meeting @ {soon:%a %d %b %Y} 3pm - 3:30pm (BST)")
    s.upsert_messages([m])
    s.create_loop(source="gmail", thread_id="i3", type="reply", person="Mahan Agabegi", summary="Reply to Mahan",
                  done_when="", due=datetime.now(timezone.utc).isoformat(), cost=30, consequence="minor", reversible=1,
                  hard_deadline=0, effort_h=0.05, stakes="")
    detect(s, RuleDecisions(), "gmail", "i3", [m])
    assert [r["type"] for r in s.loops()] == ["meeting"]
