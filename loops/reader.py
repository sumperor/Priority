"""One reading of each email: what it is, what you need to do, by when, and the link to do it.

With an API key, Claude (a small, fast model) reads each new email once and answers in a fixed shape.
Without one, the rules in engine/invites/actions decide, exactly as before. Either way the decision is
saved in the `readings` table so you can review it (right or wrong) and correct it, and your corrections
are shown to Claude as examples next time.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

from . import config as C
from .store import now_iso

KINDS = ("meeting", "action", "reply", "assessment", "job_alert", "receipt", "fyi")
LABELS = {"meeting": "A meeting or event", "action": "Something to do", "reply": "Someone waiting on a reply",
          "assessment": "An online test or assessment", "job_alert": "Job alert", "receipt": "Receipt or confirmation, nothing to do",
          "fyi": "Just information, nothing to do"}

SYSTEM = """You read one email for a busy student who is job hunting, and decide what (if anything) they must do.
Classify it as exactly one kind:
- meeting: an event at a fixed time they will attend (invite, booking, registration, interview slot, appointment)
- action: a company or service asks them to do something (update payment, top up credits, verify, renew, pay, sign, fill in, collect)
- reply: a real person is waiting for them to reply or decide
- assessment: an online test, video interview or case study to complete for an application
- job_alert: a list of job adverts
- receipt: a confirmation of something already done (order placed, payment received, application received)
- fyi: newsletters, marketing, updates that need nothing
Rules: be concrete and short. The task is an instruction starting with a verb, naming the company or person
("Update your payment method for Netflix", "Reply to Priya about the slides"). Use only dates and times written
in the email; if none, leave them empty. The link must be copied exactly from the email and must be the one to do
the task (a join link for meetings). Never ask "did you reply" about an automated email; ask about the thing.
Reply ONLY with JSON:
{"kind": "...", "task": "...", "who": "person or company", "due": "ISO datetime or empty",
 "event_start": "ISO datetime or empty", "event_end": "ISO datetime or empty", "place": "",
 "link": "", "close_question": "Did you ...?",
 "followups": [{"question": "", "kind": "choice"|"text", "options": ["2 to 4 short options for choice"]}],
 "why": "one short sentence on what the email says"}"""


def enabled():
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _examples(store, n=8):
    """Your corrections, newest first, as examples of what you meant."""
    rows = store.db.execute("SELECT subject, sender, fix FROM readings WHERE fix IS NOT NULL ORDER BY created DESC LIMIT ?",
                            (n,)).fetchall()
    out = []
    for r in rows:
        f = json.loads(r["fix"])
        out.append(f'- "{r["subject"]}" from {r["sender"]}: {LABELS.get(f.get("kind"), f.get("kind"))}'
                   + (f', task: {f["summary"]}' if f.get("summary") else ""))
    return "\n".join(out)


def _iso(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if not d.tzinfo:
        d = d.astimezone()
    return d.astimezone(timezone.utc)


def read(store, msgs):
    """Claude's reading of the latest incoming message in a thread, checked and cleaned; None if it can't."""
    from .llm import ask_json
    last = msgs[-1]
    earlier = "\n".join(f"{'Me' if m.is_from_me else m.sender_name}: {m.text[:600]}" for m in msgs[-4:-1])
    ex = _examples(store)
    user = (f"Now: {datetime.now().astimezone().isoformat(timespec='minutes')}\n"
            f"Sent: {last.ts.astimezone().isoformat(timespec='minutes')}\n"
            f"From: {last.sender_name} <{last.sender}>\nSubject: {last.subject}\n\n{last.text[:8000]}"
            + (f"\n\nEarlier in the thread:\n{earlier}" if earlier else "")
            + (f"\n\nHow this person corrected earlier readings (follow the same judgement):\n{ex}" if ex else ""))
    try:
        d = ask_json(SYSTEM, user, max_tokens=700, model=C.READER_MODEL)
    except Exception as e:
        print(f"[sparrow] reading fell back to rules: {e}")
        return None
    kind = d.get("kind") if d.get("kind") in KINDS else None
    if not kind:
        return None
    link = (d.get("link") or "").strip()
    if link and link not in (last.text or ""):
        link = ""                      # only links that are really in the email
    fu = []
    for q in (d.get("followups") or [])[:2]:
        if not q.get("question") or (q.get("kind") == "choice" and not q.get("options")):
            continue
        fu.append({"question": q["question"][:160], "kind": "choice" if q.get("kind") == "choice" else "text",
                   "options": [str(o)[:40] for o in (q.get("options") or [])[:4]], "use": "note"})
    return {"kind": kind, "task": (d.get("task") or last.subject or "")[:120], "who": (d.get("who") or last.sender_name)[:60],
            "due": _iso(d.get("due")), "start": _iso(d.get("event_start")), "end": _iso(d.get("event_end")),
            "place": (d.get("place") or "")[:80], "link": link,
            "close": {"happened": (d.get("close_question") or "")[:160], "followups": fu}, "why": (d.get("why") or "")[:200]}


def record(store, m, kind, summary, loop_id, by, detail=None):
    """Save what Sparrow decided about this email, for the review screen."""
    store.db.execute(
        "INSERT INTO readings(source,msg_id,thread_id,subject,sender,ts,kind,summary,loop_id,by,detail,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,msg_id) DO UPDATE SET kind=excluded.kind, summary=excluded.summary, "
        "loop_id=excluded.loop_id, by=excluded.by, detail=excluded.detail",
        (m.source, m.msg_id, m.thread_id, m.subject or "", f"{m.sender_name} <{m.sender}>", m.ts.isoformat(), kind,
         summary or "", loop_id, by, json.dumps(detail or {}, default=str), now_iso()))
    store.db.commit()


def act(store, source, thread_id, msgs, r):
    """Turn a reading into a loop. Returns the loop id, or None when there's nothing to do."""
    from .actions import org_of
    last = msgs[-1]
    now = datetime.now(timezone.utc)
    kind, i = r["kind"], None
    existing = store.db.execute("SELECT id FROM loops WHERE source=? AND thread_id=? AND status NOT IN ('closed','deleted') "
                                "ORDER BY id DESC LIMIT 1", (source, thread_id)).fetchone()
    if existing:
        return existing["id"]          # already on your list from an earlier email in this thread
    if kind == "meeting" and r["start"] and (r["end"] or r["start"]) > now and not store.open_loop(source, thread_id, "meeting"):
        dur = ((r["end"] - r["start"]).total_seconds() / 3600) if r["end"] and r["end"] > r["start"] else 1.0
        i = store.create_loop(source=source, thread_id=thread_id, type="meeting", person=r["who"], summary=r["task"],
                              done_when="It happened", due=r["start"].isoformat(), cost=55, consequence="relationship",
                              reversible=0, hard_deadline=1, effort_h=round(min(dur, 12), 2), stakes="It's at a fixed time.",
                              trigger_ts=last.ts.isoformat())
        note = f"{r['start'].astimezone():%a %d %b, %H:%M}" + (f", {r['place']}" if r["place"] else "")
        store.update_loop(i, note=note)
    elif kind in ("action", "reply") and not store.open_loop(source, thread_id, "task" if kind == "action" else "reply"):
        due = r["due"] if r["due"] and r["due"] > now else now + timedelta(hours=48 if kind == "action" else C.DEFAULT_DUE_HOURS.get("reply", 24))
        i = store.create_loop(source=source, thread_id=thread_id, type="task" if kind == "action" else "reply",
                              person=r["who"] if kind == "reply" else (r["who"] or org_of(last)), summary=r["task"],
                              done_when=r["close"]["happened"] or "", due=due.isoformat(), cost=45 if kind == "action" else 40,
                              consequence="minor", reversible=1, hard_deadline=int(bool(r["due"])), effort_h=0.25,
                              stakes="", trigger_ts=last.ts.isoformat())
    if i:
        f = {"link": r["link"] or None}
        if r["close"]["happened"]:
            f["close_json"] = json.dumps(r["close"])
        store.update_loop(i, **f)
    return i


def muted(store, m):
    """You said emails like this from this sender need nothing: don't make tasks from them again."""
    who = f"{m.sender_name} <{m.sender}>"
    rows = store.db.execute("SELECT fix FROM readings WHERE sender=? AND fix IS NOT NULL", (who,)).fetchall()
    return any(json.loads(r["fix"]).get("kind") in ("fyi", "receipt") for r in rows)


def review(store, days=7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = store.db.execute("SELECT * FROM readings WHERE ts >= ? ORDER BY ts DESC LIMIT 400", (since,)).fetchall()
    items = [dict(r) for r in rows]
    for it in items:
        it["detail"] = json.loads(it["detail"] or "{}")
        it["fix"] = json.loads(it["fix"]) if it["fix"] else None
        it["label"] = LABELS.get(it["kind"], it["kind"])
    marked = [it for it in items if it["verdict"] in ("right", "wrong")]
    right = sum(1 for it in marked if it["verdict"] == "right")
    return {"items": items, "marked": len(marked), "right": right, "labels": LABELS,
            "accuracy": round(100 * right / len(marked)) if marked else None}
