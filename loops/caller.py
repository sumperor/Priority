"""Phone calls, as the last resort: an ElevenLabs voice agent rings you (through your Twilio number)
when a deadline is close and you haven't responded to the reminders.

It only calls when all of these hold:
  - the task should already have started (its latest safe start has passed) and the deadline is still ahead
  - at least 2 reminders went out since you last responded, the latest over 5 minutes ago
  - you're not in a calendar event, not in a focus session, and it isn't quiet hours
  - it hasn't already called about this task
Afterwards Sparrow reads the call transcript and treats what you said like a typed reply.
Every number the agent says (minutes left, minutes needed) is worked out here and passed in.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from . import config as C
from .store import now_iso

API = "https://api.elevenlabs.io/v1/convai"
MIN_NUDGES, WAIT_AFTER_NUDGE = 2, timedelta(minutes=5)
last = {"error": ""}


def settings():
    d = C.read_secret("calls.json")
    d.setdefault("enabled", False)
    for k in ("phone", "agent_id", "phone_number_id"):
        d.setdefault(k, "")
    d.setdefault("quiet_from", 22)
    d.setdefault("quiet_to", 8)
    return d


def save(d):
    C.write_secret("calls.json", d)


def api_key():
    return os.getenv("ELEVENLABS_API_KEY") or C.ELEVENLABS_API_KEY


def ready(st=None):
    st = st or settings()
    return bool(st["enabled"] and st["phone"] and st["agent_id"] and st["phone_number_id"] and api_key())


def _quiet(st, now):
    h = now.astimezone().hour
    a, b = int(st["quiet_from"]), int(st["quiet_to"])
    return (h >= a or h < b) if a > b else (a <= h < b)


def in_event(events, now):
    for e in events:
        s = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
        en = datetime.fromisoformat((e.get("end") or e["start"]).replace("Z", "+00:00"))
        if s <= now < en:
            return e
    return None


def unanswered(store, loop):
    """Reminders sent since you last replied, and when the latest one went out."""
    rows = store.db.execute("SELECT role, ts FROM chat WHERE loop_id=? ORDER BY id", (loop["id"],)).fetchall()
    n, latest = 0, None
    for r in rows:
        if r["role"] == "you":
            n, latest = 0, None
        elif r["role"] == "agent":
            n, latest = n + 1, datetime.fromisoformat(r["ts"])
    if loop.get("started_at") or (loop.get("commit_at") and datetime.fromisoformat(loop["commit_at"]) > datetime.now(timezone.utc)):
        return 0, latest  # you said you're on it, or when you'll start
    return n, latest


def why_not(store, loop, now, events, session_active):
    """None if Sparrow should call about this loop now, else the reason it won't (for the log and tests)."""
    st = settings()
    if not ready(st):
        return "calls not set up"
    if loop["status"] != "open" or loop["type"] in ("waiting", "meeting") or loop.get("past_deadline"):
        return "not a task to call about"
    due, ls = datetime.fromisoformat(loop["due"]), datetime.fromisoformat(loop["forecast"]["latest_start"])
    if not (ls <= now < due):
        return "deadline not close yet" if now < ls else "deadline passed"
    n, latest = unanswered(store, loop)
    if n < MIN_NUDGES or not latest or now - latest < WAIT_AFTER_NUDGE:
        return "reminders not ignored yet"
    if store.checked(f"call:{loop['id']}"):
        return "already called"
    if session_active:
        return "in a focus session"
    if _quiet(st, now):
        return "quiet hours"
    ev = in_event(events, now)
    if ev:
        return f"busy: {ev['title']}"
    return None


def call(store, loop, now=None, test=False):
    """Ring the user. Returns the conversation id. Numbers come from the loop, not the model."""
    st = settings()
    now = now or datetime.now(timezone.utc)
    due = datetime.fromisoformat(loop["due"])
    left = max(0, round((due - now).total_seconds() / 60))
    need = round((loop.get("effort_adj") or loop.get("effort_h") or 0.5) * 60)
    variables = {"task": loop["summary"], "deadline": f"{due.astimezone():%A %H:%M}", "minutes_left": str(left),
                 "minutes_needed": str(need), "short_by": str(max(0, need - left)), "why_it_matters": loop.get("stakes") or "",
                 "is_test": "yes" if test else "no"}
    r = requests.post(f"{API}/twilio/outbound-call", timeout=20, headers={"xi-api-key": api_key()},
                      json={"agent_id": st["agent_id"], "agent_phone_number_id": st["phone_number_id"], "to_number": st["phone"],
                            "conversation_initiation_client_data": {"dynamic_variables": variables}})
    if not r.ok:
        raise RuntimeError(f"ElevenLabs said {r.status_code}: {r.text[:200]}")
    conv = r.json().get("conversation_id")
    if not test:
        store.mark_checked(f"call:{loop['id']}")
        store.add_chat(loop["id"], "agent", f"Called you about this: {left} min left for about {need} min of work.")
    store.db.execute("INSERT INTO summaries(loop_id,kind,body,created) VALUES(?,?,?,?)",
                     (loop["id"] if not test else 0, "call", json.dumps({"conversation_id": conv, "status": "calling"}), now_iso()))
    store.db.commit()
    return conv


def follow_up(store):
    """Read finished calls and act on what you said, exactly like a typed reply."""
    from .agent import respond
    rows = store.db.execute("SELECT id, loop_id, body FROM summaries WHERE kind='call'").fetchall()
    for r in rows:
        d = json.loads(r["body"])
        if d.get("status") != "calling" or not d.get("conversation_id"):
            continue
        try:
            g = requests.get(f"{API}/conversations/{d['conversation_id']}", headers={"xi-api-key": api_key()}, timeout=20)
            g.raise_for_status()
            info = g.json()
        except Exception as e:
            last["error"] = str(e)[:200]
            continue
        if info.get("status") not in ("done", "failed"):
            continue
        said = " ".join(t.get("message") or "" for t in info.get("transcript", []) if t.get("role") == "user").strip()
        d.update(status=info.get("status"), said=said, seconds=(info.get("metadata") or {}).get("call_duration_secs"))
        store.db.execute("UPDATE summaries SET body=? WHERE id=?", (json.dumps(d), r["id"]))
        store.db.commit()
        loop = store.get_loop(r["loop_id"]) if r["loop_id"] else None
        if loop and said:
            respond(store, loop, said, use_llm=bool(os.getenv("ANTHROPIC_API_KEY")))
        elif loop:
            store.add_chat(r["loop_id"], "agent", "I called but didn't get an answer.")


def tick(store, rows, events, session_active, now=None):
    """Called on the reminder timer. Makes at most one call per tick."""
    now = now or datetime.now(timezone.utc)
    if ready():
        try:
            follow_up(store)
        except Exception as e:
            last["error"] = str(e)[:200]
    for loop in rows:
        if why_not(store, loop, now, events, session_active) is None:
            try:
                return call(store, loop, now)
            except Exception as e:
                last["error"] = str(e)[:200]
                return None
    return None
