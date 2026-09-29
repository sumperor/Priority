"""Local app server: binds to 127.0.0.1 only, so your data never leaves this machine
except the excerpts sent to Claude for decisions."""
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config as C
from .engine import ranked, sync
from .store import Store

app = FastAPI(title="Loops")
from .session import router as session_router, active as _active_session, db as _session_db  # noqa: E402
app.include_router(session_router)
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
            sync(db(), [ALL[n]() for n in C.CONNECTORS if n in ALL], get_engine())
        _sync_state["log"] = buf.getvalue().strip()
    except Exception as e:
        _sync_state["log"] = f"Sync failed: {e}"
    finally:
        _sync_state["running"] = False
        _sync_state["last"] = datetime.now(timezone.utc).isoformat()


def _scheduler():
    while True:
        _run_sync()
        time.sleep(C.AUTO_SYNC_MINUTES * 60)


@app.on_event("startup")
def start_scheduler():
    if C.AUTO_SYNC_MINUTES > 0 and C.CONNECTORS:
        threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/session")
def session_page():
    return FileResponse(STATIC / "session.html")


@app.get("/api/state")
def state():
    s = db()
    closed = [dict(r) for r in s.closed_loops()]
    import os
    return {"loops": ranked(s), "closed": closed, "accuracy": s.accuracy(), "sync": _sync_state,
            "connectors": C.CONNECTORS, "ai": bool(os.getenv("ANTHROPIC_API_KEY")),
            "last_msg": {str(r["loop_id"]): r["text"] for r in s.db.execute(
                "SELECT loop_id, text FROM chat WHERE id IN (SELECT MAX(id) FROM chat WHERE role='agent' GROUP BY loop_id)")},
            "chase_every": C.CHASE_EVERY_MINUTES,
            "session": (lambda a: a["title"] if a else None)(_active_session(_session_db()))}


class Capture(BaseModel):
    text: str
    importance: int | None = None  # 1 low, 2 normal, 3 high


@app.post("/api/capture")
def capture(body: Capture):
    from .extract import capture as parse
    if not body.text.strip():
        raise HTTPException(400, "Empty")
    d = parse(body.text.strip())  # importance is decided by the algorithm, never by the user
    due = d.get("due") or (datetime.now(timezone.utc) +
                           timedelta(hours=C.DEFAULT_DUE_HOURS.get(d["type"], 48))).isoformat()
    s = db()
    loop_id = s.create_loop(source="manual", thread_id="", type=d["type"], person=d.get("person", ""),
                            summary=d["summary"], done_when=d.get("done_when", ""), due=due, cost=d["cost"],
                            consequence=d.get("consequence", "minor"), reversible=int(bool(d.get("reversible", True))),
                            hard_deadline=int(bool(d.get("hard_deadline", False))), effort_h=d["effort_h"],
                            stakes=d.get("stakes", ""))
    return {"id": loop_id, "questions": followups(d)}


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
    outcome, actual, notes, created = "done", None, [], []
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
        else:
            notes.append(f"{a.question} {v}")
    s.close_loop(loop_id, outcome=outcome, actual_h=actual)
    if notes:
        s.update_loop(loop_id, note=" | ".join(notes))
    s.label_decision(loop_id, "detect", True)
    return {"created": created}


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


@app.patch("/api/loops/{loop_id}")
def edit(loop_id: int, body: Edit):
    s = db(); _loop(s, loop_id)
    _apply(s, loop_id, {k: v for k, v in body.model_dump().items() if v is not None})
    return {"ok": True}


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
    if f:
        s.update_loop(loop_id, **f)
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
    return {"changed": sorted(applied)}


@app.post("/api/nudges/tick")
def nudges_tick():
    from .agent import due_nudges
    s = _session_db()
    if _active_session(s):
        return {"nudges": []}  # no nagging mid-session; the session screen shows what's pressing
    return {"nudges": due_nudges(s, ranked(s))}


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
    if not C.CONNECTORS:
        raise HTTPException(400, "No connectors configured")
    threading.Thread(target=_run_sync, daemon=True).start()
    return {"started": True}


@app.get("/api/brief")
def brief():
    from .brief import write_brief
    return {"text": write_brief(db())}


def serve():
    import uvicorn
    print(f"Loops v0.8 running at http://127.0.0.1:{C.PORT}")
    uvicorn.run(app, host="127.0.0.1", port=C.PORT, log_level="warning")
