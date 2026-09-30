"""Bookings, confirmations and 'can we meet?' emails go on the calendar as meetings."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.models import Message

NOW = datetime.now(timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from loops.store import Store
    return Store(str(tmp_path / "m.db"))


def run(store, *msgs):
    from loops.decisions import RuleDecisions
    from loops.engine import sync

    class F:
        name = "gmail"
        def fetch(self, since): return list(msgs)
    sync(store, [F()], RuleDecisions())
    return [dict(r) for r in store.loops()]


def mail(mid, sender, name, subject, text, ago=timedelta(hours=2)):
    return Message("gmail", mid, mid, sender, name, False, text, NOW - ago, subject)


def day_ahead(n):
    d = (NOW + timedelta(days=n)).astimezone()
    return d, f"{d:%A} {d.day} {d:%B} {d.year}"


def test_booking_confirmation_becomes_a_meeting(store):
    d, words = day_ahead(3)
    loops = run(store, mail("b1", "noreply@eventbrite.com", "Eventbrite", "You're registered for Careers Fair 2026",
                            f"Thanks for registering! Careers Fair 2026, {words} at 6:00 PM. We look forward to seeing you."))
    m = [l for l in loops if l["type"] == "meeting"]
    assert len(m) == 1 and m[0]["summary"] == "Careers Fair 2026"
    start = datetime.fromisoformat(m[0]["due"]).astimezone()
    assert (start.date(), start.hour, start.minute) == (d.date(), 18, 0)
    assert not [l for l in loops if l["type"] == "reply"]              # a confirmation needs no reply


def test_someone_asking_to_meet_goes_on_the_calendar_and_asks_you_to_confirm(store):
    loops = run(store, mail("a1", "priya@example.com", "Priya", "Coffee?",
                            "Hey! Can we meet tomorrow at 3pm to go over the slides?", ago=timedelta(minutes=30)))
    m = [l for l in loops if l["type"] == "meeting"]
    assert len(m) == 1 and m[0]["summary"] == "Meeting with Priya, to confirm" and "Reply to confirm" in m[0]["note"]
    start = datetime.fromisoformat(m[0]["due"]).astimezone()
    sent = (NOW - timedelta(minutes=30)).astimezone()
    assert start.date() == sent.date() + timedelta(days=1) and start.hour == 15


def test_past_events_and_plain_newsletters_are_ignored(store):
    old = (NOW - timedelta(days=2)).astimezone()
    loops = run(store,
                mail("p1", "noreply@eventbrite.com", "Eventbrite", "Your booking is confirmed",
                     f"See you on {old:%A} {old.day} {old:%B} {old.year} at 6pm"),
                mail("n1", "news@shop.com", "Shop", "Big autumn sale", "Everything half price. Sale ends Friday 3 October."))
    assert not [l for l in loops if l["type"] == "meeting"]


def test_each_email_is_only_turned_into_a_meeting_once(store):
    d, words = day_ahead(4)
    m = mail("b2", "bookings@gym.com", "PureGym", "Booking confirmed: Spin class", f"Spin class, {words} at 07:30.")
    run(store, m)
    loops = run(store, m)
    assert len([l for l in loops if l["type"] == "meeting"]) == 1
