"""Proof that a loop is done, found in your own inbox before anyone asks you.

Rules only (no API key needed). Strong evidence closes the loop and says well done;
weaker evidence marks it "looks done" for a one-tap confirm. You can always say "not done yet".
"""
import json
import re
from datetime import datetime, timedelta

ACK = re.compile(r"(thank(s| you) for (your )?(applying|application|applying for|your interest in)|"
                 r"application (has been |was )?(received|submitted|complete)|"
                 r"we('ve| have) (received|got) your application|successfully (submitted|applied)|"
                 r"your application (to|for) .{0,80} (has been|was) (received|submitted))", re.I)
ASSESS_DONE = re.compile(r"(thank(s| you) for completing|you('ve| have) (now )?(successfully )?completed|"
                         r"(assessment|test|interview|questionnaire) (has been |was )?(completed|submitted|received)|"
                         r"we('ve| have) received your (responses|assessment|video interview|submission))", re.I)
INTERVIEW_DONE = re.compile(r"(thank(s| you) for (attending|interviewing|coming in|taking the time|your time|speaking with)|"
                            r"great (to meet|meeting|speaking with) you|following your (recent )?interview)", re.I)
NEXT_STAGE = re.compile(r"(invite you to|invitation to|pleased to (invite|inform|offer)|next (stage|round|step)|"
                        r"assessment cent(re|er)|final round|second round|offer of employment|we('d| would) like to offer)", re.I)
REJECT = re.compile(r"(unfortunately|regret to inform|not (be )?(progressing|moving forward|taking your application)|"
                    r"unsuccessful|other candidates|decided not to proceed|position has been filled)", re.I)
ATS = ("workday", "myworkday", "greenhouse", "lever.co", "smartrecruiters", "icims", "taleo", "successfactors",
       "ashbyhq", "workable", "teamtailor", "oleeo", "tal.net", "applied", "jobvite", "bamboohr", "pinpoint",
       "hirevue", "shl", "pymetrics", "cappfinity", "codility", "hackerrank", "arctic shores", "sova", "talogy")
_STOP = {"i", "the", "a", "my", "me", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
         "today", "tomorrow", "graduate", "london", "uk", "cv", "it", "this", "that", "them", "him", "her"}
_SUFFIX = re.compile(r"\b(ltd|limited|plc|inc|llp|llc|group|holdings|uk|careers|recruitment|talent|team|hr)\b\.?", re.I)
PREP = re.compile(r"\b(prep|prepare|preparing|practi[cs]e|revise|research)\b", re.I)


def kind_of(loop):
    s = f"{loop['summary']} {loop.get('stakes') or ''}".lower()
    t = loop["type"]
    if t == "assessment" or re.search(r"\b(assessment|hirevue|shl|online test|numerical|verbal reasoning|codility|hackerrank|pymetrics)\b", s):
        return "assessment"
    if t == "waiting" and re.search(r"\b(hear back|application|applied|recruiter|interview)\b", s):
        return "hear_back"
    if re.search(r"\b(apply|application|submit)\b", s) and not PREP.search(loop["summary"]):
        return "application"
    if re.search(r"\binterview\b", s) and not PREP.search(loop["summary"]):
        return "interview"
    if loop.get("source") not in (None, "", "manual") and loop.get("thread_id"):
        return "thread_them" if t == "waiting" else "thread_me" if t in ("reply", "promise") else None
    return None


def org_of(loop):
    """The company or person a loop is about, e.g. 'Apply to Monzo' -> 'Monzo'."""
    if loop.get("person"):
        return _SUFFIX.sub("", loop["person"]).strip(" ,.-") or loop["person"]
    s = loop["summary"]
    for pat in (r"\b(?:at|to|with|from|for)\s+((?:[A-Z][\w&'.-]*)(?:\s+[A-Z][\w&'.-]*){0,3})",
                r"\b((?:[A-Z][\w&'.-]*)(?:\s+[A-Z][\w&'.-]*){0,2})\s+(?:application|assessment|interview|online test)"):
        for cand in reversed(re.findall(pat, s)):
            if cand.split()[0].lower() not in _STOP:
                return cand.strip()
    return ""


def mentions(org, m):
    if not org or len(org) < 3:
        return False
    hay = f"{m.sender_name} {m.sender} {m.subject} {m.text[:3000]}".lower()
    name = org.lower()
    if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", hay):
        return True
    # monzo.com, careers@monzo.co.uk
    squashed = re.sub(r"[^a-z0-9]", "", name)
    domain = m.sender.split("@")[-1].lower() if "@" in (m.sender or "") else ""
    return bool(squashed) and len(squashed) >= 4 and squashed in re.sub(r"[^a-z0-9.]", "", domain)


def _is_ats(m):
    hay = f"{m.sender} {m.sender_name}".lower()
    return any(a in hay for a in ATS)


def _label(m):
    subj = (m.subject or m.text[:80]).strip().replace("\n", " ")
    return {"from": m.sender_name or m.sender, "subject": subj[:120], "ts": m.ts.isoformat(), "source": m.source,
            "msg_id": m.msg_id}


def check_loop(store, loop):
    """Return evidence for this loop, or None. Only looks at messages already synced."""
    loop = dict(loop)
    kind = kind_of(loop)
    if not kind:
        return None
    created = datetime.fromisoformat(loop.get("trigger_ts") or loop["created"])
    org = org_of(loop)

    def fresh(m):
        return not store.checked(f"evidence:{loop['id']}:{m.source}:{m.msg_id}")

    if kind in ("thread_me", "thread_them"):
        mine = kind == "thread_me"
        later = [m for m in store.thread_messages(loop["source"], loop["thread_id"])
                 if m.ts > created and m.is_from_me == mine and fresh(m)
                 and not store.checked(f"close:{loop['id']}:{m.msg_id}")]  # already judged by autoclose
        if not later:
            return None
        who = loop.get("person") or "them"
        head = f"Looks like you replied to {who}." if mine else f"{who} got back to you."
        return {"kind": kind, "strength": "likely", "headline": head, **_label(later[-1])}

    if not org:
        return None
    since = created - timedelta(hours=1)
    msgs = [m for m in reversed(store.messages_since(since)) if fresh(m) and mentions(org, m)]
    for m in msgs:
        text = f"{m.subject}\n{m.text}"
        if kind == "application" and (ACK.search(text) or (_is_ats(m) and re.search(r"\bapplication\b", text, re.I))):
            return {"kind": kind, "strength": "strong", "org": org,
                    "headline": f"Nice one. Your {org} application went in.", **_label(m)}
        if kind == "application" and (NEXT_STAGE.search(text) or REJECT.search(text)):
            return {"kind": kind, "strength": "strong", "org": org,
                    "headline": f"Your {org} application went in, and they've already replied.", **_label(m)}
        if kind == "assessment" and (ASSESS_DONE.search(text) or NEXT_STAGE.search(text)):
            return {"kind": kind, "strength": "strong", "org": org,
                    "headline": f"Nice one. The {org} assessment is done.", **_label(m)}
        if kind == "interview" and (INTERVIEW_DONE.search(text) or NEXT_STAGE.search(text) or REJECT.search(text)):
            return {"kind": kind, "strength": "strong", "org": org,
                    "headline": f"Looks like the {org} interview happened.", **_label(m)}
        if kind == "hear_back" and not ACK.search(text) and (NEXT_STAGE.search(text) or REJECT.search(text)
                                                             or INTERVIEW_DONE.search(text)):
            news = "good news" if NEXT_STAGE.search(text) else "a decision" if REJECT.search(text) else "a reply"
            return {"kind": kind, "strength": "strong", "org": org,
                    "headline": f"{org} got back to you with {news}.", **_label(m)}
    return None


def apply(store, loop, ev):
    """Record the evidence. Strong: close it and celebrate. Likely: ask for a one-tap confirm."""
    store.mark_checked(f"evidence:{loop['id']}:{ev['source']}:{ev['msg_id']}")
    p = 0.95 if ev["strength"] == "strong" else 0.7
    store.log_decision(loop["id"], f"Evidence ({ev['kind']}) shows it's done", p, "rules", kind="close")
    if ev["strength"] == "strong":
        store.close_loop(loop["id"], outcome="evidence", confidence=p)
        store.update_loop(loop["id"], evidence=json.dumps(ev), acked=0)
        store.add_chat(loop["id"], "agent", ev["headline"])
        return "closed"
    store.update_loop(loop["id"], status="pending_close", close_confidence=p, evidence=json.dumps(ev))
    return "confirm"


def sweep(store):
    """Check every open loop. Runs after each sync and before any reminder goes out."""
    out = []
    for loop in store.loops("open"):
        ev = check_loop(store, loop)
        if ev:
            out.append((loop["id"], apply(store, loop, ev), ev))
    return out
