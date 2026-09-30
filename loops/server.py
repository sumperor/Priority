"""Local app server: binds to 127.0.0.1 only, so your data never leaves this machine
except the excerpts sent to Claude for decisions."""
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config as C
from .engine import ranked, sync
from .store import Store, now_iso

app = FastAPI(title="Sparrow")
from .session import router as session_router, active as _active_session, db as _session_db  # noqa: E402
app.include_router(session_router)
from . import connect as _connect  # noqa: E402
app.include_router(_connect.router)
_connect.after_connect = lambda: threading.Thread(target=_run_sync, daemon=True).start()
STATIC = Path(__file__).parent / "static"
_sync_state = {"running": False, "last": None, "log": ""}


def db():
    return Store(C.DB_PATH)


def _run_sync():
    if _sync_state["running"]:
        return
    _sync_state["running"] = True
    try:
        from .connectors import ALL
        from .decisions import get_engine
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sync(db(), [ALL[n]() for n in _connect.connected() if n in ALL], get_engine())
        _sync_state["log"] = buf.getvalue().strip()
    except Exception as e:
        _sync_state["log"] = f"Sync failed: {e}"
    finally:
        _sync_state["running"] = False
        _sync_state["last"] = datetime.now(timezone.utc).isoformat()


def _sources():
    """Per account: when it was last checked, and whether that worked."""
    from .connectors import LABELS
    from .engine import SOURCES
    names = LABELS
    out = []
    for n in _connect.offered():
        st = SOURCES.get(n)
        if not _connect.is_connected(n):
            state = "off"
        else:
            state = ("checking" if _sync_state["running"] and not st else "waiting" if not st
                     else "ok" if st["ok"] else "error")
        err, fix = _plain_error((st or {}).get("error", ""), names.get(n, n))
        out.append({"name": n, "label": names.get(n, n), "state": state, "at": (st or {}).get("at"),
                    "count": (st or {}).get("count"), "new_only": (st or {}).get("new_only"),
                    "note": (st or {}).get("note", ""), "error": err, "fix": fix})
    return out


def _plain_error(err, label):
    """Turn a raw API error into (what happened, what to do), in plain words."""
    e = err.lower()
    if not err:
        return "", ""
    if "quota" in e or "rate limit" in e or "ratelimit" in e or " 429" in e or "too many" in e:
        return (f"{label} is limiting how fast Sparrow can read.", "Nothing to do. It catches up on the next check.")
    if "not connected" in e or "not set up" in e:
        return (f"{label} isn't connected any more.", "Disconnect it, then connect it again.")
    if "invalid_grant" in e or "expired" in e or "revoked" in e or "authori" in e or "connect it" in e:
        return (f"The {label} sign-in has expired.", "Tap Reconnect to sign in again.")
    if "has not been used" in e or "is disabled" in e or "accessnotconfigured" in e:
        return (f"The {label} API isn't turned on for your Google Cloud project.",
                "Turn on the Gmail API and Google Calendar API in Google Cloud, then tap Check now.")
    if "full disk access" in e:
        return (err, "")
    if "connection" in e or "timed out" in e or "name resolution" in e:
        return ("Couldn't reach it. Are you online?", "It tries again at the next check.")
    return (err[:160], "")


def _scheduler():
    """Check every few minutes; while new mail keeps arriving, check every minute until it goes quiet."""
    while True:
        _run_sync()
        from .engine import SOURCES
        busy = any((st or {}).get("count") for st in SOURCES.values())
        time.sleep(60 if busy else C.AUTO_SYNC_MINUTES * 60)


@app.on_event("startup")
def start_scheduler():
    if C.AUTO_SYNC_MINUTES > 0 and C.CONNECTORS:
        threading.Thread(target=_scheduler, daemon=True).start()
    threading.Thread(target=_nudger, daemon=True).start()   # reminders even with the browser closed


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/welcome")
def welcome():
    return FileResponse(STATIC / "welcome.html")


@app.get("/pricing")
def pricing():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/welcome#pricing")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


class SignUp(BaseModel):
    name: str = ""
    email: str
    password: str
    plan: str = "free"


class LogIn(BaseModel):
    email: str
    password: str


def _with_session(token, user):
    from fastapi.responses import JSONResponse
    r = JSONResponse({"user": user})
    r.set_cookie("sparrow_session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 60)
    return r


@app.post("/api/account/signup")
def account_signup(body: SignUp):
    from . import account
    try:
        return _with_session(*account.signup(body.name, body.email, body.password, body.plan))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/account/login")
def account_login(body: LogIn):
    from . import account
    try:
        return _with_session(*account.login(body.email, body.password))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/account")
def account_me(request: Request):
    from . import account
    return {"user": account.me(request.cookies.get("sparrow_session"))}


@app.post("/api/account/logout")
def account_logout(request: Request):
    from fastapi.responses import JSONResponse
    from . import account
    account.logout(request.cookies.get("sparrow_session"))
    r = JSONResponse({"ok": True})
    r.delete_cookie("sparrow_session")
    return r


@app.get("/session")
def session_page():
    return FileResponse(STATIC / "index.html")  # focus mode now lives on the main page


@app.get("/api/state")
def state():
    s = db()
    import json, os
    from .areas import AREAS
    from .leads import shown
    _merge_duplicates(s)
    closed = [dict(r) for r in s.closed_loops()]
    loops = ranked(s)
    for l in loops + closed:
        l["evidence"] = json.loads(l["evidence"]) if l.get("evidence") else None
        if l["evidence"]:
            l["evidence"]["link"] = open_link(l["evidence"].get("source"), l["evidence"].get("msg_id"))
    for l in loops:
        l["origin"] = _origin(s, l)
    leads = shown(s)
    for x in leads:
        m = s.db.execute("SELECT * FROM messages WHERE source=? AND msg_id=?", (x["source"], x["msg_id"])).fetchone()
        x["origin"] = _msg_info(m) if m else None
    celebrate = [l for l in closed if l["outcome"] == "evidence" and not l.get("acked")]
    return {"loops": loops, "closed": closed, "celebrate": celebrate, "leads": leads, "areas": AREAS,
            "sync_every": C.AUTO_SYNC_MINUTES, "sources": _sources(), "accuracy": s.accuracy(), "sync": _sync_state,
            "connectors": _connect.connected(), "ai": bool(os.getenv("ANTHROPIC_API_KEY")),
            "last_msg": {str(r["loop_id"]): r["text"] for r in s.db.execute(
                "SELECT loop_id, text FROM chat WHERE id IN (SELECT MAX(id) FROM chat WHERE role='agent' GROUP BY loop_id)")},
            "chase_every": C.CHASE_EVERY_MINUTES,
            "session": (lambda a: a["title"] if a else None)(_active_session(_session_db()))}


def open_link(source, msg_id, thread_id=None):
    """A link that opens the original message, where the service allows it."""
    if source == "gmail" and (thread_id or msg_id):
        acct = C.read_secret("gmail_account.json").get("email")
        from urllib.parse import quote
        return f"https://mail.google.com/mail/?authuser={quote(acct)}#all/{thread_id or msg_id}" if acct \
            else f"https://mail.google.com/mail/u/0/#all/{thread_id or msg_id}"
    if source == "outlook":
        return "https://outlook.live.com/mail/"
    if source == "teams":
        return "https://teams.microsoft.com/"
    if source == "slack":
        return "https://app.slack.com/client"
    return None


def _msg_info(m):
    import re
    from .invites import clean, is_invite, parse, preview
    from .models import Message
    msg = Message(m["source"], m["msg_id"], m["thread_id"], m["sender"], m["sender_name"], bool(m["is_from_me"]),
                  m["text"] or "", datetime.fromisoformat(m["ts"]), m["subject"] or "")
    inv = parse(msg) if is_invite(msg) else None
    text = preview(inv) if inv else re.sub(r"\s+", " ", clean(m["text"] or "")).strip()
    return {"source": m["source"], "from": m["sender_name"] or m["sender"], "address": m["sender"],
            "subject": (f"Invite: {inv['title']}" if inv else m["subject"] or ""), "ts": m["ts"], "preview": text[:220] + ("\u2026" if len(text) > 220 else ""),
            "link": open_link(m["source"], m["msg_id"], m["thread_id"])}


def _origin(s, loop):
    """The message a loop came from: who sent it, the subject, when it arrived, and a preview."""
    if loop.get("source") in (None, "", "manual") or not loop.get("thread_id"):
        return None
    rows = s.db.execute("SELECT * FROM messages WHERE source=? AND thread_id=? ORDER BY ts",
                        (loop["source"], loop["thread_id"])).fetchall()
    if not rows:
        return None
    t = loop.get("trigger_ts")
    m = next((r for r in rows if r["ts"] == t), None) or \
        next((r for r in reversed(rows) if not t or r["ts"] <= t), rows[-1])
    return _msg_info(m)


class Capture(BaseModel):
    text: str
    importance: int | None = None  # 1 low, 2 normal, 3 high


_GAVE_REASON = __import__("re").compile(r"\b(because|cause|so that|so i|otherwise|or else|in case|before (my|the)|for (my|the|an?|his|her|their)\s+\w+)", __import__("re").I)


@app.post("/api/capture")
def capture(body: Capture):
    from .extract import capture as parse
    if not body.text.strip():
        raise HTTPException(400, "Empty")
    d = parse(body.text.strip())  # importance is decided by the algorithm, never by the user
    d["summary"] = tidy_summary(d["summary"])
    if not _GAVE_REASON.search(body.text):
        d["stakes"] = ""   # don't invent why it matters; the card asks instead
    due = d.get("due") or (datetime.now(timezone.utc) +
                           timedelta(hours=C.DEFAULT_DUE_HOURS.get(d["type"], 48))).isoformat()
    s = db()
    dup = _same_task(s, d["summary"])
    if dup:
        return {"id": dup, "duplicate": True, "questions": []}
    from .errands import HAS_TIME, is_errand, parse as errand_parse
    errand = is_errand(body.text)
    known = errand_parse(body.text) if errand else {}
    loop_id = s.create_loop(source="manual", thread_id="", type=d["type"], person=d.get("person", ""),
                            summary=d["summary"], done_when=d.get("done_when", ""), due=due, cost=d["cost"],
                            consequence=d.get("consequence", "minor"), reversible=int(bool(d.get("reversible", True))),
                            hard_deadline=int(bool(d.get("hard_deadline", False))), effort_h=d["effort_h"],
                            stakes=d.get("stakes", ""))
    if not errand:
        return {"id": loop_id, "questions": followups(d)}
    # errands: keep what the note said, and never assume a day it didn't give
    if d.get("due"):
        when = datetime.fromisoformat(d["due"])
        known["errand_day"] = when.astimezone().date().isoformat()
        if HAS_TIME.search(body.text):
            known["start_at"] = when.isoformat()
    _apply(s, loop_id, known)
    return {"id": loop_id, "errand": True, "questions": []}


@app.get("/api/loops/{loop_id}/next")
def errand_next(loop_id: int):
    """The next errand question (one at a time), or the finished estimate."""
    from .errands import next_step
    from .maps import mac_location, origin, set_here, travel
    from .planner import calendar_events
    s = db(); l = dict(_loop(s, loop_id))
    if not origin() and l.get("place") and l.get("commit_at") and not l.get("place_ok"):
        set_here(mac_location())   # the Mac's own location, when the browser didn't give one
    now = datetime.now(timezone.utc)
    try:
        events = calendar_events(now - timedelta(hours=1), now + timedelta(days=15))
    except Exception:
        events = []
    return next_step(l, origin(), events, now, lookup=lambda place, mode, start: travel(place, mode, start))


_WHEN_TAIL = __import__("re").compile(
    r"[\s,]*(\b(today|tonight|tomorrow( morning| afternoon| evening| night)?|this (morning|afternoon|evening|week|weekend)|"
    r"next week|(on |by |this |next )?(mon|tues|wednes|thurs|fri|satur|sun)day|(at|by|before|around) \d{1,2}([:.]\d{2})?\s*(am|pm)?|"
    r"in (\d+|a|an|one|two|few|couple)( of)? (minutes?|mins?|hours?|days?|weeks?))\b[\s,.]*)+$", __import__("re").I)


def tidy_summary(text):
    """'Buy groceries tomorrow' -> 'Buy groceries': the when is shown separately."""
    t = _WHEN_TAIL.sub("", text.strip()).strip(" ,.")
    return (t[:1].upper() + t[1:]) if len(t) >= 3 else text.strip()


def _merge_duplicates(s):
    """Same task typed twice (from older versions, or a double tap): keep the first, drop the rest quietly."""
    import re
    seen = {}
    for r in sorted(s.loops("open"), key=lambda r: r["id"]):
        if r["source"] != "manual":
            continue
        k = re.sub(r"[^a-z0-9 ]", "", tidy_summary(r["summary"]).lower()).strip()
        if k in seen:
            s.close_loop(r["id"], outcome="dismissed")
            s.update_loop(r["id"], note=f"Duplicate of task {seen[k]}")
        else:
            seen[k] = r["id"]
            if tidy_summary(r["summary"]) != r["summary"]:
                s.update_loop(r["id"], summary=tidy_summary(r["summary"]))


def _same_task(s, summary):
    """An open task with the same wording already exists (double tap, or said twice)."""
    import re
    norm = lambda x: re.sub(r"[^a-z0-9 ]", "", x.lower()).strip()
    n = norm(summary)
    for r in s.loops():
        if r["source"] == "manual" and norm(tidy_summary(r["summary"])) == n:
            return r["id"]
    return None


def followups(d):
    """Ask only what's missing and what changes the plan: how long it takes, and by when."""
    qs, now = [], datetime.now().astimezone()
    who = f" with {d['person']}" if d.get("person") else ""
    if not d.get("effort_stated") and d["type"] != "waiting":
        if d["type"] == "call":
            q, opts = f"How long do you think the call{who} might take?", [("5 min", 5), ("15 min", 15), ("30 min", 30), ("An hour", 60)]
        elif d["type"] == "reply":
            q, opts = "How long will the reply take?", [("2 min", 2), ("10 min", 10), ("30 min", 30), ("An hour", 60)]
        else:
            q, opts = "How long do you think this will take?", [("15 min", 15), ("30 min", 30), ("1 hour", 60), ("2 hours", 120), ("Half a day", 240)]
        qs.append({"field": "effort_h", "question": q,
                   "options": [{"label": l, "value": round(m / 60, 3)} for l, m in opts]})
    if not d.get("due_stated") and d["type"] != "waiting":
        today = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if today <= now + timedelta(hours=1):
            today = now + timedelta(hours=2)
        tomorrow = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        week = (now + timedelta(days=(4 - now.weekday()) % 7 or 7)).replace(hour=17, minute=0, second=0, microsecond=0)
        q = f"When does the call{who} need to happen by?" if d["type"] == "call" else "When does this need to be done by?"
        opts = [("Within the hour", now + timedelta(hours=1)), ("Today", today), ("Tomorrow", tomorrow),
                ("This week", week), ("No real deadline", now + timedelta(days=7))]
        qs.append({"field": "due", "question": q,
                   "options": [{"label": l, "value": v.astimezone(timezone.utc).isoformat()} for l, v in opts]})
    return qs


class Close(BaseModel):
    outcome: str = "well"          # well | partly | no
    actual_h: float | None = None
    mattered: str | None = None    # more | as_expected | less


def _loop(s, loop_id):
    l = s.get_loop(loop_id)
    if not l:
        raise HTTPException(404, "No such loop")
    return l


@app.post("/api/loops/{loop_id}/close")
def close(loop_id: int, body: Close):
    s = db(); _loop(s, loop_id)
    s.close_loop(loop_id, outcome=body.outcome, actual_h=body.actual_h)
    if body.mattered:
        s.update_loop(loop_id, mattered=body.mattered)
    s.label_decision(loop_id, "detect", True)  # it was a real loop
    return {"ok": True}


@app.get("/api/loops/{loop_id}/closing")
def closing(loop_id: int):
    from .closing import closing_questions
    s = db()
    return closing_questions(dict(_loop(s, loop_id)))


class Answer(BaseModel):
    question: str
    use: str
    value: str


class Finish(BaseModel):
    happened: str                  # yes | no
    answers: list[Answer] = []


def _create_from_text(s, text):
    from .extract import capture as parse
    d = parse(text)
    due = d.get("due") or (datetime.now(timezone.utc) +
                           timedelta(hours=C.DEFAULT_DUE_HOURS.get(d["type"], 48))).isoformat()
    loop_id = s.create_loop(source="manual", thread_id="", type=d["type"], person=d.get("person", ""),
                            summary=d["summary"], done_when=d.get("done_when", ""), due=due, cost=d["cost"],
                            consequence=d.get("consequence", "minor"), reversible=int(bool(d.get("reversible", True))),
                            hard_deadline=int(bool(d.get("hard_deadline", False))), effort_h=d["effort_h"],
                            stakes=d.get("stakes", ""))
    return {"id": loop_id, "summary": d["summary"], "questions": followups(d)}


@app.post("/api/loops/{loop_id}/finish")
def finish(loop_id: int, body: Finish):
    """Close a loop from the step-by-step check-in."""
    s = db(); l = _loop(s, loop_id)
    if body.happened == "no":
        s.close_loop(loop_id, outcome="dropped")
        s.label_decision(loop_id, "detect", True)
        return {"created": []}
    outcome, actual, notes, created, interview_notes = "done", None, [], [], ""
    for a in body.answers:
        v = a.value.strip()
        if not v:
            continue
        if a.use == "outcome":
            outcome = v
        elif a.use == "effort":
            est = l["effort_h"] or 0.25
            actual = est * {"Quicker than planned": 0.7, "About as planned": 1.0, "Longer than planned": 1.5}.get(v, 1.0)
        elif a.use == "next_loop" and v.lower() not in ("no", "nothing", "none", "n/a"):
            created.append(_create_from_text(s, v))
        elif a.use == "interview_notes":
            interview_notes = v
        else:
            notes.append(f"{a.question} {v}")
    s.close_loop(loop_id, outcome=outcome, actual_h=actual)
    if notes:
        s.update_loop(loop_id, note=" | ".join(notes))
    s.label_decision(loop_id, "detect", True)
    from .interviews import is_interview
    write_up = None
    if is_interview(l):
        from .interviews import draft
        write_up = draft(s, l, interview_notes, use_ai=_ai())
        created.append(_thank_you_loop(s, l))
    return {"created": created, "write_up": write_up}


def _thank_you_loop(s, l):
    from .evidence import org_of
    who = org_of(dict(l)) or "them"
    due = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    i = s.create_loop(source="manual", thread_id="", type="task", person=l["person"] or "", area="Jobs",
                      summary=f"Send a thank-you note to {who}", done_when="Thank-you email sent",
                      due=due.astimezone(timezone.utc).isoformat(), cost=35, consequence="relationship", reversible=1,
                      hard_deadline=0, effort_h=0.15, stakes="A short thank-you within a day keeps you front of mind.")
    return {"id": i, "summary": f"Send a thank-you note to {who}", "questions": []}


class Reschedule(BaseModel):
    due: str


@app.post("/api/loops/{loop_id}/reschedule")
def reschedule(loop_id: int, body: Reschedule):
    s = db(); l = _loop(s, loop_id)
    from .extract import norm_due
    s.update_loop(loop_id, due=norm_due(body.due) or l["due"], snoozes=(l["snoozes"] or 0) + 1)
    return {"ok": True}


@app.get("/api/when-options")
def when_options():
    now = datetime.now().astimezone()
    today = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if today <= now + timedelta(hours=1):
        today = now + timedelta(hours=2)
    opts = [("In an hour", now + timedelta(hours=1)), ("Later today", today),
            ("Tomorrow", (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)),
            ("This week", (now + timedelta(days=(4 - now.weekday()) % 7 or 7)).replace(hour=17, minute=0, second=0, microsecond=0))]
    return [{"label": l, "value": v.astimezone(timezone.utc).isoformat()} for l, v in opts]


@app.post("/api/loops/{loop_id}/confirm")
def confirm(loop_id: int):
    s = db(); l = _loop(s, loop_id)
    s.close_loop(loop_id, outcome="auto_confirmed", confidence=l["close_confidence"])
    s.label_decision(loop_id, "close", True); s.label_decision(loop_id, "detect", True)
    return {"ok": True}


@app.post("/api/loops/{loop_id}/reject")
def reject(loop_id: int):
    s = db(); _loop(s, loop_id)
    s.update_loop(loop_id, status="open")
    s.label_decision(loop_id, "close", False)
    return {"ok": True}


@app.post("/api/loops/{loop_id}/dismiss")
def dismiss(loop_id: int):
    """Not a real task: closes it and teaches detection it was wrong."""
    s = db(); _loop(s, loop_id)
    s.close_loop(loop_id, outcome="dismissed")
    s.label_decision(loop_id, "detect", False)
    return {"ok": True}


@app.delete("/api/loops/{loop_id}")
def delete_loop(loop_id: int):
    """Delete button: gone from the list and the calendar. Kept for a while so Undo works."""
    s = db(); _loop(s, loop_id)
    s.update_loop(loop_id, status="deleted", closed_at=now_iso())
    return {"ok": True}


@app.post("/api/loops/{loop_id}/restore")
def restore_loop(loop_id: int):
    s = db(); l = _loop(s, loop_id)
    if l["status"] == "deleted":
        s.update_loop(loop_id, status="open", closed_at=None)
    return {"ok": True}


@app.post("/api/loops/{loop_id}/reopen")
def reopen(loop_id: int):
    s = db(); l = _loop(s, loop_id)
    if l["outcome"] == "auto":
        s.label_decision(loop_id, "close", False)  # auto-close was wrong
    s.update_loop(loop_id, status="open", outcome=None, closed_at=None, on_time=None)
    return {"ok": True}


@app.post("/api/loops/{loop_id}/snooze")
def snooze(loop_id: int):
    s = db(); l = _loop(s, loop_id)
    due = max(datetime.fromisoformat(l["due"]), datetime.now(timezone.utc))
    s.update_loop(loop_id, due=(due + timedelta(days=1)).isoformat(), snoozes=(l["snoozes"] or 0) + 1)
    return {"ok": True}


class Edit(BaseModel):
    summary: str | None = None
    person: str | None = None
    due: str | None = None
    cost: int | None = None
    effort_h: float | None = None
    stakes: str | None = None
    blocks: int | None = None       # 0 clears
    hard_deadline: bool | None = None
    reversible: bool | None = None
    start_at: str | None = None      # errands: when you'll go
    travel_mode: str | None = None   # walk | cycle | drive | transit | delivery
    travel_min: int | None = None    # one way
    place: str | None = None
    there_min: int | None = None     # errands: time at the place
    home: str | None = None          # where errands set off from (saved once, not on the loop)
    here: str | None = None          # "lat,lon" from the browser's location, used as the starting point
    errand_day: str | None = None    # YYYY-MM-DD picked on the calendar
    place_ok: bool | None = None     # you confirmed the place found on the map


@app.patch("/api/loops/{loop_id}")
def edit(loop_id: int, body: Edit):
    s = db(); _loop(s, loop_id)
    f = _apply(s, loop_id, {k: v for k, v in body.model_dump().items() if v is not None})
    l = s.get_loop(loop_id)
    return {"ok": True, "set": {k: l[k] for k in ("travel_min",) if l[k] is not None}, "route": f.get("_route"), "route_error": f.get("_route_error")}


def _apply(s, loop_id, f):
    if "blocks" in f:
        f["blocks"] = f["blocks"] or None
        if f["blocks"] == loop_id:
            raise HTTPException(400, "A loop can't block itself")
    if "due" in f:
        d = datetime.fromisoformat(f["due"])
        f["due"] = (d if d.tzinfo else d.astimezone()).astimezone(timezone.utc).isoformat()
    for k in ("hard_deadline", "reversible"):
        if k in f:
            f[k] = int(f[k])
    if "cost" in f:
        f["cost"] = max(1, min(100, int(f["cost"])))
    if "effort_h" in f:
        f["effort_h"] = max(0.02, float(f["effort_h"]))
    if f.get("start_at") == "__day":         # "Another day": back to the calendar
        f.pop("start_at")
        f.update(errand_day=None, commit_at=None)
    if f.get("place") == "__skipmap":       # couldn't find it: carry on without the map
        f.pop("place")
        f["place_ok"] = 1
    if f.get("travel_mode") == "__wrong":    # "Wrong place": ask where again
        f.pop("travel_mode")
        f.update(place=None, place_ok=0, travel_min=None)
    if "place_ok" in f:
        f["place_ok"] = int(bool(f["place_ok"]))
    if "start_at" in f:
        d = datetime.fromisoformat(f.pop("start_at"))
        f["commit_at"] = (d if d.tzinfo else d.astimezone()).astimezone(timezone.utc).isoformat()
    if f.get("travel_min") is not None:
        f["travel_min"] = max(0, min(180, int(f["travel_min"])))
    if f.get("place") == "delivery":
        f.update(place=None, travel_mode="delivery")
    elif f.get("place"):
        f["place"] = f["place"].strip()[:80]
    if "home" in f:
        from .maps import set_home
        set_home(f.pop("home"))
    if "here" in f:
        from .maps import set_here
        set_here(f.pop("here"))
    if "there_min" in f:
        f["base_h"] = round(max(1, min(600, int(f.pop("there_min")))) / 60, 3)
    if f:
        s.update_loop(loop_id, **f)
    cur = dict(s.get_loop(loop_id))
    if ({"travel_mode", "place"} & set(f)) and cur.get("place") and cur.get("travel_mode") not in (None, "delivery") \
            and "travel_min" not in f and (cur.get("travel_min") is None or "travel_mode" in f):
        from .maps import travel
        route = travel(cur["place"], cur["travel_mode"])  # looked up, not guessed
        if not route:
            from .maps import last as maps_last
            f["_route_error"] = maps_last["error"]
            if "travel_mode" in f:   # the old mode's minutes don't apply any more
                s.update_loop(loop_id, travel_min=None)
                cur["travel_min"] = None
        if route:
            s.update_loop(loop_id, travel_min=route["minutes"])
            cur["travel_min"] = f["travel_min"] = route["minutes"]
            f["_route"] = route
    if ({"travel_mode", "travel_min", "place", "base_h"} & set(f)) or ("commit_at" in f and (cur.get("base_h") or cur.get("travel_mode") or cur.get("errand_day"))):
        from .errands import recompute
        s.update_loop(loop_id, **recompute(cur))
    return f


def _ai():
    import os
    return bool(os.getenv("ANTHROPIC_API_KEY"))


class Text(BaseModel):
    text: str


@app.post("/api/loops/{loop_id}/revise")
def revise_loop(loop_id: int, body: Text):
    """'No, I meant tomorrow at 10' -> field changes, applied straight away."""
    from .agent import revise
    s = db(); l = _loop(s, loop_id)
    ch = revise(l, body.text, use_llm=_ai())
    if ch.get("due"):
        from .extract import norm_due
        ch["due"] = norm_due(ch["due"])
        if not ch["due"]:
            ch.pop("due")
    for k in ("hard_deadline", "reversible"):
        if k in ch:
            ch[k] = bool(ch[k])
    applied = _apply(s, loop_id, dict(ch)) if ch else {}
    return {"changed": sorted(k for k in applied if not k.startswith("_"))}


@app.post("/api/loops/{loop_id}/ack")
def ack(loop_id: int):
    """'Nice' on a well-done card: the evidence was right."""
    s = db(); _loop(s, loop_id)
    s.update_loop(loop_id, acked=1)
    s.label_decision(loop_id, "close", True); s.label_decision(loop_id, "detect", True)
    return {"ok": True}


class NotDone(BaseModel):
    left: str = ""        # what's still to do, in your words
    due: str | None = None


@app.post("/api/loops/{loop_id}/not-done")
def not_done(loop_id: int, body: NotDone):
    """'Not done yet': reopen, narrow the task to what's left, and set when it's due."""
    s = db(); l = _loop(s, loop_id)
    import json
    ev = json.loads(l["evidence"]) if l["evidence"] else {}
    if l["status"] in ("closed", "pending_close"):
        s.label_decision(loop_id, "close", False)
    f = {"status": "open", "outcome": None, "closed_at": None, "on_time": None, "acked": 1, "evidence": None,
         "close_confidence": None, "last_nudge": None}
    left = body.left.strip().rstrip(".")
    import re
    task = re.sub(r"^(that was only (the )?(first )?part[.,]?\s*)?(i('ve)? )?(still )?(need|have|got) to\s+|^still to do:?\s*", "", left, flags=re.I)
    if left:
        task = task or left
        f["summary"] = (task[:1].upper() + task[1:])[:120]
        seen = f"Saw \u201c{ev.get('subject')}\u201d from {ev.get('from')}. " if ev else ""
        f["note"] = f"{seen}Was: {l['summary']}. Still to do: {left}."
    if body.due:
        from .extract import norm_due
        f["due"] = norm_due(body.due) or l["due"]
    s.update_loop(loop_id, **f)
    s.add_chat(loop_id, "you", f"Not done yet. {left}".strip())
    return {"ok": True}


@app.get("/api/days-options")
def days_options():
    now = datetime.now().astimezone()
    out = []
    for n in range(1, 6):
        d = (now + timedelta(days=n)).replace(hour=17, minute=0, second=0, microsecond=0)
        out.append({"label": "Tomorrow" if n == 1 else f"{d:%A}" if n < 7 else f"In {n} days",
                    "value": d.astimezone(timezone.utc).isoformat()})
    return out


class Notes(BaseModel):
    notes: str


@app.post("/api/loops/{loop_id}/write-up")
def write_up(loop_id: int, body: Notes):
    from .interviews import draft
    s = db(); l = _loop(s, loop_id)
    return draft(s, l, body.notes, use_ai=_ai())


@app.post("/api/loops/{loop_id}/explore")
def explore_loop(loop_id: int, fresh: bool = False):
    """Who is this person and what might the meeting be about. Reuses the last result unless fresh."""
    from .explore import cached, explore
    s = db(); l = _loop(s, loop_id)
    return (None if fresh else cached(s, loop_id)) or explore(s, l, use_ai=_ai())


# ---------------------------------------------------------------- the calendar
@app.get("/api/calendar")
def calendar(start: str, days: int = 7):
    from .planner import _utc, build
    s = db()
    return build(ranked(s), _utc(start), max(1, min(42, days)))


class Move(BaseModel):
    start: str


@app.post("/api/loops/{loop_id}/move")
def move(loop_id: int, body: Move):
    """Dragged on the calendar: keep that time, hold you to it, and say if it's too late."""
    from .planner import _utc, move_warning
    s = db(); l = _loop(s, loop_id)
    t = _utc(body.start)
    s.update_loop(loop_id, commit_at=t.isoformat(), last_nudge=None)
    eff = (l["effort_h"] or 0.25) * s.effort_multiplier(l["type"])
    return {"ok": True, "warning": move_warning(dict(l), t, eff)}


# ---------------------------------------------------------------- jobs from your inbox
@app.get("/api/profile")
def get_profile():
    from . import jobs
    s = _session_db()
    cv, li = jobs._doc(s, "cv"), jobs._doc(s, "linkedin")
    return {"compare_with": jobs.prefs()["compare_with"], "cv": cv[0], "linkedin": li[0], "ai": _ai(),
            "interests": jobs.prefs().get("interests", "")}


class Compare(BaseModel):
    compare_with: str | None = None
    interests: str | None = None      # roles, sectors and places you want, in your words


@app.post("/api/profile")
def set_profile(body: Compare):
    from . import jobs
    if body.compare_with is not None:
        jobs.set_compare(body.compare_with)
    if body.interests is not None:
        jobs.set_interests(body.interests)
    return get_profile()


class Url(BaseModel):
    url: str


@app.post("/api/profile/linkedin-url")
def linkedin_url(body: Url):
    from . import jobs
    import re
    if not re.match(r"^https?://(www\.)?linkedin\.com/in/[^/\s]+", body.url.strip()):
        raise HTTPException(400, "Paste your profile link. It looks like linkedin.com/in/your-name.")
    if not _ai():
        raise HTTPException(400, "Reading LinkedIn needs your Claude API key. Or upload the LinkedIn PDF instead.")
    text, warn = jobs.linkedin_from_url(_session_db(), body.url.strip())
    if not text:
        raise HTTPException(400, warn)
    return {"ok": True, "chars": len(text), "warning": warn}


class Scan(BaseModel):
    days: int = 14


@app.post("/api/jobs/scan")
def jobs_scan(body: Scan):
    from . import jobs
    if not _connect.connected():
        raise HTTPException(400, "Connect Gmail or Outlook first, so there's an inbox to look through.")
    jobs.start_scan(_session_db, max(1, min(90, body.days)))
    return jobs.scan_state


@app.get("/api/jobs")
def jobs_list():
    from . import jobs
    s = _session_db()
    out = jobs.all_jobs(s)
    for x in out:
        m = s.db.execute("SELECT * FROM messages WHERE source=? AND msg_id=?", (x["source"], x["msg_id"])).fetchone()
        x["origin"] = _msg_info(m) if m else None
    emails = len({(x["source"], x["msg_id"]) for x in out})
    return {"jobs": out, "emails": emails, "scan": jobs.scan_state, "check": jobs.check_state, "profile": get_profile()}


@app.post("/api/jobs/check-all")
def jobs_check_all():
    """Check the relevance of every job not checked yet, in the background."""
    from . import jobs
    jobs.check_all(_session_db)
    return jobs.check_state


@app.post("/api/leads/{lead_id}/review")
def lead_review(lead_id: int):
    from . import jobs
    r = jobs.review(_session_db(), lead_id)
    if r is None:
        raise HTTPException(404, "No such job")
    return r


@app.post("/api/leads/{lead_id}/cv")
def lead_cv(lead_id: int):
    from . import jobs
    try:
        return jobs.tailor_cv(_session_db(), lead_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _cv(sid):
    import json
    r = _session_db().db.execute("SELECT body FROM summaries WHERE id=? AND kind='cv'", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "No such CV")
    return json.loads(r["body"])


@app.get("/cv/{sid}.docx")
def cv_word(sid: int):
    from fastapi.responses import Response
    from . import jobs
    cv = _cv(sid)
    name = (cv.get("name") or "CV").replace(" ", "_")
    return Response(jobs.cv_docx(cv), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{name}_CV.docx"'})


@app.get("/cv/{sid}")
def cv_page(sid: int):
    from fastapi.responses import HTMLResponse
    from . import jobs
    return HTMLResponse(jobs.cv_html(_cv(sid)))


@app.post("/api/leads/{lead_id}/apply")
def lead_apply(lead_id: int):
    from .leads import accept
    s = db()
    i = accept(s, lead_id)
    if not i:
        raise HTTPException(404, "No such lead")
    return {"id": i}


@app.post("/api/leads/{lead_id}/skip")
def lead_skip(lead_id: int):
    s = db()
    s.db.execute("UPDATE leads SET status='skipped' WHERE id=?", (lead_id,)); s.db.commit()
    return {"ok": True}


def run_nudges():
    """Work out due reminders and send them to the Mac and phone. Called by the page and a background timer."""
    from .agent import due_nudges
    from .notify import dispatch
    s = _session_db()
    busy = bool(_active_session(s))
    rows = ranked(s)
    nudges = [] if busy else due_nudges(s, rows)   # no nagging mid-session
    dispatch(nudges)
    _maybe_call(s, rows, busy)
    return nudges


def _maybe_call(s, rows, busy):
    """Last resort: a phone call when a close deadline's reminders are being ignored."""
    from . import caller
    if not caller.ready():
        return
    from .planner import calendar_events
    now = datetime.now(timezone.utc)
    try:
        events = calendar_events(now - timedelta(hours=12), now + timedelta(hours=12))
    except Exception:
        events = []
    rows = [{**r, **dict(s.get_loop(r["id"]))} for r in rows if r["status"] == "open"]
    caller.tick(s, rows, events, busy, now)


def _nudger():
    while True:
        time.sleep(30)
        try:
            run_nudges()
        except Exception as e:
            print(f"[sparrow] reminders: {e}")


@app.post("/api/nudges/tick")
def nudges_tick():
    return {"nudges": run_nudges()}


@app.get("/api/calls")
def calls_settings():
    from . import caller
    st = caller.settings()
    return {**st, "api_key": bool(caller.api_key()), "ready": caller.ready(st), "error": caller.last["error"],
            "phone": st["phone"]}


class CallSettings(BaseModel):
    enabled: bool | None = None
    phone: str | None = None
    agent_id: str | None = None
    phone_number_id: str | None = None
    quiet_from: int | None = None
    quiet_to: int | None = None


@app.post("/api/calls")
def calls_update(body: CallSettings):
    import re
    from . import caller
    st = caller.settings()
    f = {k: v for k, v in body.model_dump().items() if v is not None}
    if "phone" in f:
        p = re.sub(r"[\s()-]", "", f["phone"])
        if p.startswith("07"):
            p = "+44" + p[1:]   # UK mobile written the usual way
        if p and not re.fullmatch(r"\+\d{8,15}", p):
            raise HTTPException(400, "Use your full mobile number, like +44 7700 900123.")
        f["phone"] = p
    for k in ("agent_id", "phone_number_id"):
        if k in f:
            f[k] = f[k].strip()
    for k in ("quiet_from", "quiet_to"):
        if k in f:
            f[k] = max(0, min(23, int(f[k])))
    st.update(f)
    caller.save(st)
    return calls_settings()


@app.post("/api/calls/test")
def calls_test():
    from . import caller
    st = caller.settings()
    missing = [name for name, ok in (("your mobile", st["phone"]), ("the agent ID", st["agent_id"]),
                                     ("the phone number ID", st["phone_number_id"]),
                                     ("ELEVENLABS_API_KEY in .env", caller.api_key())) if not ok]
    if missing:
        raise HTTPException(400, "Still needed: " + ", ".join(missing) + ".")
    fake = {"id": 0, "summary": "a test task", "due": (datetime.now(timezone.utc) + timedelta(minutes=40)).isoformat(),
            "effort_h": 0.5, "stakes": ""}
    try:
        return {"conversation_id": caller.call(_session_db(), fake, test=True)}
    except Exception as e:
        raise HTTPException(400, f"Couldn't start the call: {e}")


@app.get("/api/notify")
def notify_settings():
    from . import notify
    import shutil
    st = notify.settings()
    return {**st, "mac_available": notify.mac_available(), "clickable": bool(shutil.which("terminal-notifier")),
            "ntfy_url": f"{notify.NTFY}/{st['ntfy_topic']}" if st["ntfy_topic"] else "", "errors": notify.last_error}


class NotifySettings(BaseModel):
    mac: bool | None = None
    ntfy: bool | None = None


@app.post("/api/notify")
def notify_update(body: NotifySettings):
    from . import notify
    st = notify.settings()
    if body.mac is not None:
        st["mac"] = body.mac
    if body.ntfy is not None:
        st["ntfy"] = body.ntfy
        if body.ntfy and not st["ntfy_topic"]:
            st["ntfy_topic"] = notify.new_topic()
    notify.save(st)
    return notify_settings()


@app.post("/api/notify/test")
def notify_test():
    from . import notify
    sent = notify.send("Sparrow", "Reminders are working. This is what a nudge looks like.",
                       url=f"http://127.0.0.1:{C.PORT}/")
    if not sent:
        raise HTTPException(400, ("Couldn't send: " + "; ".join(notify.last_error.values())) if notify.last_error
                            else "Nothing is switched on yet. Tick Mac notifications or Your phone first.")
    return {"sent": sent, "errors": notify.last_error}


@app.get("/api/loops/{loop_id}/chat")
def chat(loop_id: int):
    s = db(); _loop(s, loop_id)
    return {"messages": s.chat(loop_id)}


@app.post("/api/loops/{loop_id}/reply")
def reply(loop_id: int, body: Text):
    from .agent import respond
    s = db(); l = _loop(s, loop_id)
    if not body.text.strip():
        raise HTTPException(400, "Empty reply")
    r = respond(s, l, body.text.strip(), use_llm=_ai())
    return {**r, "messages": s.chat(loop_id)}


@app.post("/api/sync")
def sync_now():
    if not _connect.connected():
        raise HTTPException(400, "Connect an account first: tap one at the top of the page.")
    threading.Thread(target=_run_sync, daemon=True).start()
    return {"started": True}


@app.get("/api/brief")
def brief():
    from .brief import write_brief
    return {"text": write_brief(db())}


def serve():
    import uvicorn
    print(f"Sparrow v0.33 running at http://127.0.0.1:{C.PORT}")
    uvicorn.run(app, host="127.0.0.1", port=C.PORT, log_level="warning")
