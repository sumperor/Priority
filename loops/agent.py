"""The chaser: nudges you when something should have started, and answers when you reply.

Every number it tells you comes from the loop's own deadline and effort, not from the model.
"""
import re
from datetime import datetime, timedelta, timezone

from . import config as C

OPENERS = ["Are you on it?", "Still there?", "Checking in again.", "This one matters.", "Quick check."]


def utcnow():
    return datetime.now(timezone.utc)


def _t(dt):
    return dt.astimezone().strftime("%H:%M")


def _dur(minutes):
    m = max(0, round(minutes))
    h, mm = divmod(m, 60)
    return f"{h}h {mm} min" if h and mm else f"{h}h" if h else f"{mm} min"


def _q(summary):
    s = summary.strip().rstrip(".")
    if len(s) > 60:
        s = s[:60].rsplit(" ", 1)[0] + "\u2026"
    return f"\u201c{s}\u201d"


def due_nudges(store, rows):
    """Called every ~30s by the app. Returns new nudges and records them in the chat."""
    now, out = utcnow(), []
    for r in rows:
        if r["status"] != "open" or r["type"] == "waiting" or r.get("started_at"):
            continue
        if r["bucket"] == "Later" or r.get("due_guess"):
            continue                   # not urgent: the gentle reminder below keeps these moving
        ls = datetime.fromisoformat(r["forecast"]["latest_start"])
        due = datetime.fromisoformat(r["due"])
        commit = datetime.fromisoformat(r["commit_at"]) if r.get("commit_at") else None
        last = datetime.fromisoformat(r["last_nudge"]) if r.get("last_nudge") else None
        if commit and now < commit:
            continue  # you told it when you'd start; it waits until then
        effort_min = r["effort_adj"] * 60
        left_min = (due - now).total_seconds() / 60
        n = store.nudge_count(r["id"])
        if now < ls - timedelta(minutes=C.HEADS_UP_MINUTES):
            continue
        # Before chasing, look for proof it's already done (a confirmation email, a reply you sent)
        from .evidence import apply, check_loop
        ev = check_loop(store, r)
        if ev:
            if apply(store, r, ev) == "closed":
                out.append({"id": r["id"], "summary": r["summary"], "text": ev["headline"], "kind": "done"})
            continue
        if now < ls:
            if last:
                continue  # one heads-up only
            msg = (f"Heads up: {_q(r['summary'])} should start by {_t(ls)}, in {_dur((ls - now).total_seconds() / 60)}. "
                   f"Ready to start?")
        else:
            if last and (now - last) < timedelta(minutes=C.CHASE_EVERY_MINUTES):
                continue
            if commit:
                msg = f"You said you'd start {_q(r['summary'])} at {_t(commit)}. Have you started?"
            elif left_min <= 0:
                if last and last >= due and ((now - last) < timedelta(hours=C.OVERDUE_EVERY_HOURS)
                                             or _asked_since(store, r["id"], due) >= 3):
                    continue  # past deadlines: ask every few hours, 3 times at most, then leave it on the list
                msg = f"The deadline for {_q(r['summary'])} has passed. Did it happen?"
            else:
                opener = OPENERS[min(n, len(OPENERS) - 1)]
                msg = (f"{opener} {_q(r['summary'])} needed to start by {_t(ls)}. "
                       f"You have {_dur(left_min)} left and it needs about {_dur(effort_min)}.")
                if (r["cost"] or 0) >= 60 and n >= 1 and r.get("stakes"):
                    msg += f" {r['stakes']}"
        store.add_chat(r["id"], "agent", msg)
        store.update_loop(r["id"], last_nudge=now.isoformat())
        out.append({"id": r["id"], "summary": r["summary"], "text": msg})
    if not out:
        g = _gentle(store, rows, now)
        if g:
            out.append(g)
    return out


def _asked_since(store, loop_id, since):
    return store.db.execute("SELECT COUNT(*) n FROM chat WHERE loop_id=? AND role='agent' AND ts>=?",
                            (loop_id, since.isoformat())).fetchone()["n"]


def _gentle(store, rows, now):
    """Nothing urgent: every so often, bring up the next thing on the list so it doesn't sit there forever.
    One task at a time, top of the list first, not in quiet hours, not something you just added."""
    from .caller import _quiet, settings
    if _quiet(settings(), now):
        return None
    latest = store.db.execute("SELECT MAX(ts) t FROM chat WHERE role='agent'").fetchone()["t"]
    if latest and now - datetime.fromisoformat(latest) < timedelta(minutes=C.REMIND_EVERY_MINUTES):
        return None
    for r in sorted(rows, key=lambda x: (bool(x.get("due_guess")), datetime.fromisoformat(x["due"]))):   # real deadlines first, soonest first
        if r["status"] != "open" or r["type"] in ("waiting", "meeting") or r.get("started_at"):
            continue
        if datetime.fromisoformat(r["due"]) <= now:
            continue               # past deadlines are asked about above, a few times only
        if r.get("commit_at") and now < datetime.fromisoformat(r["commit_at"]):
            continue
        if r.get("created") and now - datetime.fromisoformat(r["created"]) < timedelta(minutes=30):
            continue               # you only just added it
        last = datetime.fromisoformat(r["last_nudge"]) if r.get("last_nudge") else None
        if last and now - last < timedelta(hours=C.TASK_REMIND_HOURS):
            continue
        need = _dur(r["effort_adj"] * 60)
        if r.get("due_guess"):
            msg = f"Still on your list: {_q(r['summary'])}. It takes about {need}. Do it now, or tell me when."
        else:
            due = datetime.fromisoformat(r["due"]).astimezone()
            msg = (f"Coming up: {_q(r['summary'])}, due {due:%a} {_t(due)}. It takes about {need}. "
                   f"Got a gap now, or tell me when?")
        store.add_chat(r["id"], "agent", msg)
        store.update_loop(r["id"], last_nudge=now.isoformat())
        return {"id": r["id"], "summary": r["summary"], "text": msg}
    return None


# ---------------------------------------------------------------- understanding your reply
def parse_reply(text):
    from .extract import rule_parse
    s = text.lower().strip()
    if re.search(r"\b(done|finished|did it|completed|all good now|it'?s over|went (well|fine|badly))\b", s):
        return {"intent": "done"}
    if re.search(r"\b(can'?t|won'?t|not today|skip|not doing|cancel+ed|drop it)\b", s):
        return {"intent": "skip"}
    d = rule_parse(text)
    future = re.search(r"\b(in \d+|in (a|an|one|two|five|ten|few|couple)|at \d+|by \d+|after|later|will|going to|gonna)\b|'ll\b", s)
    if d["due_stated"] and future:
        return {"intent": "later", "start": datetime.fromisoformat(d["due"]) if d["due"] else None}
    if re.search(r"\b(start(ed|ing)?|on it|doing it|studying|working on it|preparing|prepping|now)\b", s) and not future:
        return {"intent": "started"}
    return {"intent": "unknown"}


def _llm_parse(loop, text):
    from .llm import ask_json
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    d = ask_json("Classify a reply to a reminder. Reply ONLY with JSON: "
                 '{"intent": "started"|"later"|"done"|"skip"|"unknown", "start": ISO local datetime or null}',
                 f"Current time: {now}\nTask: {loop['summary']}\nReply: {text}")
    start = None
    if d.get("start"):
        from .extract import norm_due
        iso = norm_due(d["start"])
        start = datetime.fromisoformat(iso) if iso else None
    return {"intent": d.get("intent", "unknown"), "start": start}


def respond(store, loop, text, use_llm=True):
    loop = dict(loop)
    store.add_chat(loop["id"], "you", text)
    p = parse_reply(text)
    if p["intent"] == "unknown" and use_llm:
        try:
            p = _llm_parse(loop, text)
        except Exception:
            pass
    now, due = utcnow(), datetime.fromisoformat(loop["due"])
    effort = (loop["effort_h"] or 0.25) * store.effort_multiplier(loop["type"]) * 60
    stakes = loop.get("stakes") or ""
    interview = "interview" in (loop["summary"] + stakes).lower()
    action = None

    if p["intent"] == "started":
        left = (due - now).total_seconds() / 60
        store.update_loop(loop["id"], started_at=now.isoformat(), commit_at=None)
        msg = f"Good. You have {_dur(left)} until {_t(due)} for about {_dur(effort)} of work."
        msg += " That's tight, so stay on it." if left < effort * 1.25 else " I'll leave you to it."
    elif p["intent"] == "later" and p.get("start"):
        start = p["start"]
        left = round((due - start).total_seconds() / 60)
        effort = round(effort)
        store.update_loop(loop["id"], commit_at=start.isoformat())
        if left <= 0:
            msg = f"{_t(start)} is after the deadline at {_t(due)}. {stakes} Can you start sooner?".replace("  ", " ")
        elif left >= effort * 1.25:
            msg = (f"Okay, {_t(start)}. That leaves {_dur(left)} for about {_dur(effort)}, which is enough. "
                   f"I'll check in at {_t(start)}.")
        elif left >= effort:
            msg = (f"Starting at {_t(start)} leaves {_dur(left)} for about {_dur(effort)}. "
                   f"There's no room for anything to overrun. I'll check in at {_t(start)}.")
        else:
            short = effort - left
            msg = (f"Starting at {_t(start)} leaves only {_dur(left)}, and this needs about {_dur(effort)}. "
                   f"You'd be {_dur(short)} short" + (", and that could show in the interview." if interview else ".") +
                   " Can you start sooner?")
    elif p["intent"] == "done":
        msg, action = "Nice. Close the loop and I'll ask a couple of quick questions.", "close"
    elif p["intent"] == "skip":
        msg, action = "Okay. Do you want to move it to another time, or drop it?", "later"
    else:
        msg = 'Tell me when you\'ll start, like "in 10 minutes" or "at 2pm", or say "done".'
    store.add_chat(loop["id"], "agent", msg)
    store.update_loop(loop["id"], last_nudge=now.isoformat())
    return {"reply": msg, "action": action}


# ---------------------------------------------------------------- corrections ("no, I meant...")
FIELDS = ("summary", "person", "due", "effort_h", "cost", "stakes", "hard_deadline", "reversible")


def revise(loop, text, use_llm=True):
    """Turn a spoken or typed correction into field changes."""
    loop = dict(loop)
    if use_llm:
        try:
            from .llm import ask_json
            now = datetime.now().astimezone().isoformat(timespec="minutes")
            current = {k: loop.get(k) for k in FIELDS}
            d = ask_json("Apply a user's correction to a task. Reply ONLY with JSON containing just the fields that "
                         f"change, from: {list(FIELDS)}. due is ISO local datetime, effort_h hours, cost 1-100.",
                         f"Current time: {now}\nCurrent task: {current}\nCorrection: {text}")
            return {k: v for k, v in d.items() if k in FIELDS}
        except Exception:
            pass
    from .extract import rule_parse
    r, s, ch = rule_parse(text), text.lower(), {}
    if r["due_stated"]:
        ch["due"] = r["due"]
    if r["effort_stated"]:
        ch["effort_h"] = r["effort_h"]
    if r["person"]:
        ch["person"] = r["person"]
    if re.search(r"not (so |that |very )?important|no rush|low priority", s):
        ch["cost"] = 10
    elif re.search(r"\b(important|urgent|critical|matters a lot)\b", s):
        ch["cost"] = 75
    m = re.search(r"\b(?:i meant|it'?s actually|it should say|call it|rename it to)\s+(.+)", text, re.I)
    if m:
        t = m.group(1).strip().rstrip(".")
        ch["summary"] = t[:1].upper() + t[1:]
    return ch
