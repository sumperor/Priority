"""The core loop: detect -> auto-close -> prioritise."""
import os
import re
from datetime import datetime, timedelta, timezone

from .config import (AUTO_CLOSE, CONFIRM_CLOSE, DEFAULT_DUE_HOURS, DETECT_THRESHOLD,
                     LOOKBACK_DAYS, WAITING_DAYS)
from .extract import extract as default_extract


def utcnow():
    return datetime.now(timezone.utc)


def thread_state(msgs, limit=6):
    lines = []
    subj = next((m.subject for m in msgs if m.subject), "")
    if subj:
        lines.append(f"Subject: {subj}")
    for m in msgs[-limit:]:
        who = "Me" if m.is_from_me else m.sender_name
        lines.append(f"[{m.ts:%Y-%m-%d %H:%M} {m.source}] {who}: {m.text.strip()[:800]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- detection
QUESTIONS = {
    "reply": "Does the latest message need a reply or action from Me? (No for FYIs, thanks, automated mail.)",
    "promise": "In Me's latest message, did Me commit to doing or sending something later?",
    "waiting": "Is Me's latest message asking the other person for something Me is still waiting on?",
}


def _rules_extract(msgs, type_):
    """No API key (or Claude failed): build the loop from the last message with rules."""
    from .extract import rule_parse
    last = msgs[-1]
    other = next((m.sender_name for m in reversed(msgs) if not m.is_from_me), "")
    r = rule_parse(f"{last.subject}. {last.text[:600]}" if last.subject else last.text[:600])
    subj = (last.subject or r["summary"] or "the message").strip()
    summary = {"reply": f"Reply to {other or 'them'} about {subj}", "promise": f"Do what you promised: {subj}",
               "waiting": f"Hear back from {other or 'them'} about {subj}"}.get(type_, r["summary"])
    return {**r, "summary": summary[:90], "person": other, "done_when": ""}


def _create(store, decide, extract, source, thread_id, msgs, type_, p):
    try:
        d = extract(thread_state(msgs), type_)
    except Exception as e:
        print(f"[loops] extraction fell back to rules: {e}")
        d = _rules_extract(msgs, type_)
    due = d.get("due")
    if not due:
        due = (utcnow() + timedelta(hours=DEFAULT_DUE_HOURS[type_])).isoformat()
    else:
        from .extract import norm_due
        due = norm_due(due) or (utcnow() + timedelta(hours=DEFAULT_DUE_HOURS[type_])).isoformat()
    other = next((m.sender_name for m in reversed(msgs) if not m.is_from_me), "")
    loop_id = store.create_loop(
        source=source, thread_id=thread_id, type=type_, person=d.get("person") or other,
        summary=d["summary"], done_when=d.get("done_when", ""), due=due, cost=d["cost"],
        consequence=d.get("consequence", "minor"), reversible=int(bool(d.get("reversible", True))),
        hard_deadline=int(bool(d.get("hard_deadline", False))), effort_h=d["effort_h"],
        stakes=d.get("stakes", ""), trigger_ts=msgs[-1].ts.isoformat())
    store.log_decision(loop_id, QUESTIONS[type_], p, decide.name)
    return loop_id


ASSESS = re.compile(r"\b(online assessment|assessment centre|assessment center|psychometric|aptitude test|"
                    r"numerical reasoning|verbal reasoning|situational judg|hirevue|video interview|pymetrics|"
                    r"cappfinity|shl|codility|hackerrank|online test|coding test|case study exercise|game[- ]based)\b", re.I)


def is_assessment(text):
    return bool(ASSESS.search(text or ""))


def _create_assessment(store, source, thread_id, msgs, m):
    from .extract import rule_parse
    company = (m.sender_name or m.sender or "").split("<")[0].strip() or "the employer"
    behalf = re.search(r"on behalf of\s+(.+)", company, re.I)   # "SHL on behalf of Shell" -> Shell
    if behalf:
        company = behalf.group(1).strip()
    found = ASSESS.findall(f"{m.subject} {m.text}")
    generic = {"online assessment", "online test", "assessment centre", "assessment center"}
    kind = next((k for k in found if k.lower() not in generic), found[0])  # "numerical reasoning" beats "online assessment"
    due = None
    try:
        d = default_extract(thread_state(msgs), "task")
        due = d.get("due")
    except Exception:
        r = rule_parse(f"{m.subject}. {m.text[:600]}")
        due = r["due"]
    due = due or (utcnow() + timedelta(days=5)).isoformat()
    return store.create_loop(
        source=source, thread_id=thread_id, type="assessment", person=company,
        summary=f"Complete the {company} {kind.lower()}", done_when="Assessment submitted", due=due, cost=85,
        consequence="opportunity", reversible=0, hard_deadline=1, effort_h=1.0,
        stakes="If it isn't done by the deadline, the application usually ends there.",
        trigger_ts=m.ts.isoformat())


def _create_meeting(store, source, thread_id, m):
    from .invites import parse, summary
    inv = parse(m)
    if inv["start"] and (inv["end"] or inv["start"]) < utcnow():
        return None   # it already happened: nothing to do
    due = (inv["start"] or utcnow() + timedelta(hours=48)).isoformat()
    return store.create_loop(
        source=source, thread_id=thread_id, type="meeting", person=inv["organizer"], summary=summary(inv),
        done_when="The meeting happened", due=due, cost=55, consequence="relationship", reversible=0,
        hard_deadline=1, effort_h=0.17, stakes="It's at a fixed time. Missing it without a word looks bad.",
        trigger_ts=m.ts.isoformat())


def detect(store, decide, source, thread_id, msgs, extract=default_extract):
    if not msgs:
        return []
    last, created = msgs[-1], []

    def ask(type_):
        key = f"detect:{type_}:{source}:{last.msg_id}"
        if store.checked(key) or store.open_loop(source, thread_id, type_):
            return
        store.mark_checked(key)
        p = decide.yes_no(thread_state(msgs), QUESTIONS[type_])
        if p >= DETECT_THRESHOLD:
            created.append(_create(store, decide, extract, source, thread_id, msgs, type_, p))

    # Assessments and tests in your inbox become their own high-stakes loops
    incoming = [m for m in msgs if not m.is_from_me]
    if incoming and not store.open_loop(source, thread_id, "assessment"):
        m = incoming[-1]
        key = f"assess:{source}:{m.msg_id}"
        if not store.checked(key):
            store.mark_checked(key)
            if is_assessment(f"{m.subject} {m.text}"):
                created.append(_create_assessment(store, source, thread_id, msgs, m))
                if m is last:
                    store.mark_checked(f"detect:reply:{source}:{last.msg_id}")  # the assessment is the task, not a reply

    # A calendar invite is a meeting at a fixed time, not something to reply to
    from .invites import is_invite
    if not last.is_from_me and is_invite(last):
        key = f"invite:{source}:{last.msg_id}"
        if not store.checked(key):
            store.mark_checked(key)
            store.mark_checked(f"detect:reply:{source}:{last.msg_id}")
            if not store.open_loop(source, thread_id, "meeting"):
                mid = _create_meeting(store, source, thread_id, last)
                if mid:
                    created.append(mid)
            old = store.open_loop(source, thread_id, "reply")   # made by older versions: it was never a reply
            if old:
                store.close_loop(old["id"], outcome="dismissed")
                store.label_decision(old["id"], "detect", False)
        return created
    from .leads import is_job_alert, looks_like_scam
    if not last.is_from_me:
        # Job alerts become leads, not replies; recruiter emails with scam signs are never chased
        if not is_job_alert(last.sender, last.sender_name, last.subject) and not looks_like_scam(last):
            ask("reply")
    else:
        ask("promise")
        if utcnow() - last.ts >= timedelta(days=WAITING_DAYS):
            ask("waiting")
    return created


# ---------------------------------------------------------------- auto-close
def autoclose(store, decide):
    results = []
    for loop in store.loops():
        # Evidence = messages after the one that created the loop
        created = datetime.fromisoformat(loop["trigger_ts"] or loop["created"])
        msgs = store.thread_messages(loop["source"], loop["thread_id"])
        mine = loop["type"] in ("reply", "promise")
        evidence = [m for m in msgs if m.ts > created and m.is_from_me == mine]
        if not evidence:
            continue
        key = f"close:{loop['id']}:{evidence[-1].msg_id}"
        if store.checked(key):
            continue
        store.mark_checked(key)
        q = ("Does this evidence show the task is complete?" if mine
             else "Did the other person deliver what was asked? ('I'll get back to you' is not delivery.)")
        state = (f"TASK: {loop['summary']}\nDONE WHEN: {loop['done_when']}\n\nEVIDENCE:\n"
                 + "\n".join(f"[{m.ts:%Y-%m-%d %H:%M}] {'Me' if m.is_from_me else m.sender_name}: {m.text[:800]}"
                             for m in evidence[-3:]))
        p = decide.yes_no(state, q)
        store.log_decision(loop["id"], q, p, decide.name, kind="close")
        # Wrongly closing is worse than wrongly leaving open, more so for costly loops
        auto = min(0.98, AUTO_CLOSE + (0.05 if (loop["cost"] or 0) >= 70 else 0))
        if p >= auto:
            store.close_loop(loop["id"], outcome="auto", confidence=p)
            results.append((loop["id"], "closed", p))
        elif p >= CONFIRM_CLOSE:
            store.update_loop(loop["id"], status="pending_close", close_confidence=p)
            results.append((loop["id"], "confirm", p))
    return results


# ---------------------------------------------------------------- priority (EV)
def p_miss(store, loop):
    hours_left = (datetime.fromisoformat(loop["due"]) - utcnow()).total_seconds() / 3600
    effort = (loop["effort_h"] or 0.25) * store.effort_multiplier(loop["type"])
    slack_h = hours_left - effort
    p = 0.95 if slack_h < 0 else 0.6 if slack_h < 24 else 0.35 if slack_h < 72 else 0.15
    on, n = store.on_time_rate(loop["type"])
    if n >= 20:  # enough history to trust your own record
        p = 0.5 * p + 0.5 * (1 - on / n)
    return min(0.99, p + 0.05 * (loop["snoozes"] or 0))


def ranked(store):
    """Priority = P(miss) x cost / effort, with irreversible high-cost loops always first.
    Cost is adjusted by what you've told it mattered more or less than expected."""
    from .forecast import forecast
    now = utcnow()
    for l in store.loops("open"):
        if l["type"] == "meeting" and datetime.fromisoformat(l["due"]) < now - timedelta(hours=1):
            store.close_loop(l["id"], outcome="passed")   # a meeting that's over needs nothing from you
    loops = [dict(l) for l in store.loops()]
    from .areas import classify
    for l in loops:
        if not l.get("area") or l["area"] == "Other":  # made before areas existed, or rules have improved since
            a = classify(l["summary"], l["stakes"] or "", l["person"] or "", l["source"] or "", l["type"])
            if a != l.get("area"):
                l["area"] = a
                store.update_loop(l["id"], area=a)
    by_id = {l["id"]: l for l in loops}
    # A loop that holds others up carries part of their cost
    rows = []
    for loop in loops:
        effort = max(0.05, (loop["effort_h"] or 0.25) * store.effort_multiplier(loop["type"]))
        cost = min(100, (loop["cost"] or 30) * store.importance_multiplier(loop["consequence"]))
        from .forecast import knock_on
        cost_total = cost + 0.5 * sum((c["cost"] or 30) for c in knock_on(loop, by_id))
        pm = p_miss(store, loop)
        # Tier 0: can't be recovered if missed and costly. Among those, the nearest deadline goes first.
        tier = 0 if (not loop["reversible"] and cost >= 70) else 1
        rows.append({**loop, "p_miss": pm, "effort_adj": round(effort, 2), "cost_adj": round(cost_total, 1),
                     "ev_per_hour": pm * cost_total / effort, "tier": tier,
                     "forecast": forecast(loop, pm, effort, cost_total, by_id)})
    # Past their deadline by over an hour: out of the plan, into a quiet list. Asked about once, then left alone.
    past = [r for r in rows if r["type"] != "waiting" and datetime.fromisoformat(r["due"]) < now - timedelta(hours=1)]
    for r in past:
        r["past_deadline"] = True
        r["bucket"] = "Past"
        r["why"] = f"The deadline was {datetime.fromisoformat(r['due']).astimezone():%a %d %b, %H:%M}."
    return plan([r for r in rows if r not in past]) + past


def _dur(h):
    from .forecast import dur
    return dur(h)


def _short(text, n=40):
    t = text.strip().rstrip(".")
    if len(t) <= n:
        return f"\u201c{t}\u201d"
    return f"\u201c{t[:n].rsplit(' ', 1)[0]}\u2026\u201d"


def plan(rows):
    """Order loops the way a sensible person would.

    1. Pressing loops (at risk, due within a day, or unrecoverable and due within 3 days)
       are lined up by when they must start.
    2. Before each one, slot in quick tasks that fit in the spare time without endangering it
       (never more than half the spare time), highest value per hour first.
    3. Everything else follows by value per hour.
    """
    now = utcnow()
    for r in rows:
        r["_ls"] = datetime.fromisoformat(r["forecast"]["latest_start"])
    pressing = sorted([r for r in rows if r["forecast"]["level"] != "ok"
                       or (r["tier"] == 0 and r["forecast"]["hours_left"] < 72)], key=lambda r: r["_ls"])
    others = sorted([r for r in rows if r not in pressing], key=lambda r: -r["ev_per_hour"])
    # Quick pressing items can also be slotted in front of bigger pressing ones
    out, t = [], now
    queue = list(pressing)
    while queue:
        c = queue.pop(0)
        spare = (c["_ls"] - t).total_seconds() / 3600
        candidates = sorted([o for o in others + queue if o["effort_adj"] <= 0.5 and o["type"] != "meeting"],
                            key=lambda o: (o not in queue, -o["ev_per_hour"]))
        budget = spare / 2
        for o in candidates:
            if o["effort_adj"] <= budget:
                o["why"] = (f"Quick one first: about {_dur(o['effort_adj'])}. You still have time before "
                            f"{_short(c['summary'])} needs to start.")
                out.append(o)
                budget -= o["effort_adj"]
                t += timedelta(hours=o["effort_adj"])
                (queue if o in queue else others).remove(o)
        start = c["_ls"].astimezone()
        c["why"] = ("Start now: it's already tight." if c["_ls"] <= t else
                    f"Start by {start:%H:%M}" + ("" if start.date() == now.astimezone().date() else f" on {start:%A}") +
                    f". It needs about {_dur(c['effort_adj'])}.")
        out.append(c)
        t = max(t, now) + timedelta(hours=c["effort_adj"])
    for o in others:
        o["why"] = "When you have a gap."
        out.append(o)
    for r in out:
        if r["type"] == "meeting":  # a meeting happens at its time; it isn't slotted in like a task
            at = datetime.fromisoformat(r["due"]).astimezone()
            day = "Today" if at.date() == now.astimezone().date() else "Tomorrow" \
                if at.date() == (now.astimezone() + timedelta(days=1)).date() else f"{at:%A}"
            r["why"] = f"{day} at {at:%H:%M}. Be ready a few minutes before." if at > now else "This was due to start."
    first_pressing = next((i for i, r in enumerate(out) if r in pressing), -1)
    for i, r in enumerate(out):
        r.pop("_ls", None)
        r["bucket"] = ("Do now" if (r in pressing and i <= first_pressing + 1) or i < first_pressing
                       else "Today" if r in pressing or r["p_miss"] >= 0.6 else "Later")
    return out


# ---------------------------------------------------------------- orchestration
SOURCES = {}  # last check per account, shown at the top of the page: {name: {ok, at, count | error}}

def sync(store, connectors, decide, extract=default_extract):
    # Look back far enough that unanswered requests can age into 'waiting' loops
    since = utcnow() - timedelta(days=LOOKBACK_DAYS + WAITING_DAYS)
    for c in connectors:
        try:
            import inspect
            incremental = "known" in inspect.signature(c.fetch).parameters
            msgs = c.fetch(since, known=store.known_ids(c.name)) if incremental else c.fetch(since)
            store.upsert_messages(msgs)
            for mid in getattr(c, "skipped", []):
                store.mark_checked(f"seen2:{c.name}:{mid}")  # seen2: skipped under the current rules
            SOURCES[c.name] = {"ok": True, "at": utcnow().isoformat(), "count": len(msgs),
                               "new_only": incremental, "note": getattr(c, "note", "")}
            print(f"{c.name}: {len(msgs)} messages")
        except Exception as e:
            SOURCES[c.name] = {"ok": False, "at": utcnow().isoformat(), "error": str(e)[:200]}
            print(f"{c.name}: skipped ({e})")
    new = []
    for source, thread_id in store.recent_threads(since):
        new += detect(store, decide, source, thread_id, store.thread_messages(source, thread_id), extract)
    closed = autoclose(store, decide)
    # Proof in your inbox (confirmation emails, next-stage invites) closes loops before anyone asks you
    from .evidence import sweep
    closed += [(i, r) for i, r, _ in sweep(store)]
    from .leads import scan
    try:
        leads = scan(store, since, use_ai=bool(os.getenv("ANTHROPIC_API_KEY")))
    except Exception as e:
        leads = 0
        print(f"leads: skipped ({e})")
    print(f"New loops: {len(new)}. Auto-closed: {sum(1 for r in closed if r[1] == 'closed')}. "
          f"Need your confirm: {sum(1 for r in closed if r[1] == 'confirm')}. New job leads: {leads}.")
    return new, closed
