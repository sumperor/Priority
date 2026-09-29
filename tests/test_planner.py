"""The calendar plan: tasks go into free time, before their deadline, around events; dragged times are kept."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.planner import build, move_warning

LOCAL = datetime.now().astimezone().tzinfo


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=LOCAL).astimezone(timezone.utc)


def row(i, summary, due, effort_h, latest=None, **kw):
    return {"id": i, "summary": summary, "status": "open", "type": kw.pop("type", "task"), "area": "Jobs", "due": due.isoformat(),
            "effort_adj": effort_h, "forecast": {"latest_start": (latest or due - timedelta(hours=effort_h)).isoformat(), "level": "ok"}, **kw}


def test_tasks_fill_free_time_before_deadlines():
    now = at(5, 9, 7)                                   # Monday 09:07
    events = [{"title": "Lecture", "start": at(5, 9, 30).isoformat(), "end": at(5, 11).isoformat(), "source": "google"}]
    rows = [row(1, "Shell test", at(5, 17), 1.0), row(2, "Reply to Priya", at(6, 12), 0.25),
            row(3, "Meeting with Mahan", at(5, 15), 0.17, type="meeting")]
    cal = build(rows, at(5, 0), 7, events=events, now=now)
    b = {x["id"]: x for x in cal["blocks"]}
    s1, e1 = datetime.fromisoformat(b[1]["start"]), datetime.fromisoformat(b[1]["end"])
    assert s1 == at(5, 11) and e1 == at(5, 12)          # first free hour after the lecture
    s2 = datetime.fromisoformat(b[2]["start"])
    assert s2 == at(5, 9, 15) and b[2]["minutes"] == 15  # quick one fits before the lecture
    assert [m["id"] for m in cal["meetings"]] == [3] and not any(x["id"] == 3 for x in cal["blocks"])
    assert {d["id"] for d in cal["deadlines"]} == {1, 2}


def test_dragged_time_is_kept_and_nothing_overlaps():
    now = at(5, 8)
    rows = [row(1, "Apply to Monzo", at(7, 18), 1.0, commit_at=at(6, 14).isoformat()),
            row(2, "Revise stats", at(6, 20), 2.0)]
    cal = build(rows, at(5, 0), 7, events=[], now=now)
    b = {x["id"]: x for x in cal["blocks"]}
    assert b[1]["fixed"] and datetime.fromisoformat(b[1]["start"]) == at(6, 14)
    spans = sorted((datetime.fromisoformat(x["start"]), datetime.fromisoformat(x["end"])) for x in cal["blocks"])
    assert all(a[1] <= b_[0] for a, b_ in zip(spans, spans[1:]))


def test_work_only_in_waking_hours_and_late_is_flagged():
    now = at(5, 21, 30)
    rows = [row(1, "Essay", at(5, 23), 2.0)]
    b = build(rows, at(5, 0), 7, events=[], now=now)["blocks"][0]
    s = datetime.fromisoformat(b["start"]).astimezone(LOCAL)
    assert s.hour == 8 and s.day == 6 and b["late"]      # no 2-hour block fits before 22:00, so tomorrow morning, marked late


def test_move_warning():
    loop = {"due": at(5, 17).isoformat()}
    assert move_warning(loop, at(5, 14), 1.0) == ""
    assert "30 min short" in move_warning(loop, at(5, 16, 30), 1.0)
    assert "after the deadline" in move_warning(loop, at(5, 18), 1.0)
