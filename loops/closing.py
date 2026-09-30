"""Task-specific check-in when a loop is closed.

Always starts with one question: did it happen (phrased for this task).
Then 0 to 2 follow-ups, only when the answer is useful later. Trivial tasks get none.
"""
import re

from .llm import ask_json

SYSTEM = """You write the short check-in a thoughtful assistant asks when someone closes a task.
Question 1 confirms it happened, phrased for this exact task, e.g. "Did you call Yash back?".
Then 0 to 2 follow-ups ONLY if the answer is useful later:
- interviews: how it went, and whether there's a next step to follow up on
- client or work meetings, pitches, demos: anything to remember or do before the next meeting
- applications or submissions: whether a confirmation arrived
- long pieces of work: whether it took longer than planned
Everyday personal tasks (calling a friend back, replying to a message, reading) get NO follow-ups.
If the task came from a company or automated email, never ask "did you reply?"; ask whether the thing it
asked for was done (e.g. "Did you update your payment method?", "Did you top up your credits?").
Reply ONLY with JSON:
{"happened": "question text",
 "followups": [{"question": "text", "kind": "choice" or "text",
                "options": ["2 to 4 short options, choice only"],
                "use": "outcome" (how it went) | "next_loop" (answer is something to do next) | "note" | "effort" (took longer or shorter)}]}"""

NOTES_Q = {"question": "What did you talk about? Rough notes are fine, and I'll write it up with a thank-you note.",
           "kind": "text", "use": "interview_notes"}
EFFORT_OPTIONS = ["Quicker than planned", "About as planned", "Longer than planned"]


def _happened(loop):
    p, t, s = loop["person"] or "", loop["type"], loop["summary"]
    if t == "call":
        return f"Did you call {p} back?" if p else "Did the call happen?"
    if t == "reply":
        return f"Did you deal with {p}'s email?" if p else "Did you deal with this email?"
    if t == "waiting":
        return f"Did {p} get back to you?" if p else "Did they get back to you?"
    low = s.lower()
    for word in ("interview", "meeting", "call", "exam", "appointment", "session"):
        if re.search(rf"\b{word}\b", low):
            return f"Did the {word} happen?"
    return f"Did you get this done: {s[:70].rstrip('.')}?"


def _rules(loop):
    from .interviews import is_interview
    s = f"{loop['summary']} {loop['stakes'] or ''}".lower()
    f = []
    if re.search(r"\binterview", s):
        f = [{"question": "How did it go?", "kind": "choice", "options": ["Well", "Okay", "Not great"], "use": "outcome"},
             NOTES_Q if is_interview(loop) else
             {"question": "Anything to follow up on, like a thank-you note or next round?", "kind": "text", "use": "next_loop"}]
    elif re.search(r"\b(client|meeting|pitch|demo|stakeholder)\b", s):
        f = [{"question": "Anything to remember or do before the next meeting?", "kind": "text", "use": "next_loop"}]
    elif re.search(r"\b(application|apply|submit|submission)\b", s):
        f = [{"question": "Did you get a confirmation?", "kind": "choice", "options": ["Yes", "Not yet"], "use": "note"}]
    elif (loop["effort_h"] or 0) >= 1:
        f = [{"question": "How long did it take?", "kind": "choice", "options": EFFORT_OPTIONS, "use": "effort"}]
    return {"happened": _happened(loop), "followups": f}


def closing_questions(loop):
    loop = dict(loop)
    if loop.get("action"):   # made from a company email: ask about the thing it asked for
        from .actions import questions
        q = questions(loop["action"], loop)
        if q:
            return {**q, "happened_options": [{"label": "Yes, done", "value": "yes"}, {"label": "Not yet", "value": "not_yet"},
                                              {"label": "No, it's not needed", "value": "no"}]}
    try:
        d = ask_json(SYSTEM, f"Task: {loop['summary']}\nType: {loop['type']}\nWith: {loop['person'] or 'n/a'}\n"
                             f"Why it matters: {loop['stakes'] or 'n/a'}\nEstimated effort: {loop['effort_h']}h")
        fu = []
        for q in (d.get("followups") or [])[:2]:
            if q.get("kind") == "choice" and not q.get("options"):
                continue
            if q.get("use") == "effort":
                q["options"] = EFFORT_OPTIONS
            fu.append({"question": q["question"], "kind": q.get("kind", "text"),
                       "options": q.get("options", [])[:4], "use": q.get("use", "note")})
        from .interviews import is_interview
        if is_interview(loop):
            fu = [q for q in fu if q["use"] != "next_loop"][:1] + [NOTES_Q]
        out = {"happened": d.get("happened") or _happened(loop), "followups": fu}
    except Exception:
        out = _rules(loop)
    yes = ([{"label": "Yes, I replied", "value": "yes"}, {"label": "Yes, I did what it asked", "value": "yes"}]
           if loop["type"] == "reply" else [{"label": "Yes", "value": "yes"}])
    out["happened_options"] = yes + [{"label": "Not yet", "value": "not_yet"},
                                     {"label": "No, it's not needed any more", "value": "no"}]
    return out
