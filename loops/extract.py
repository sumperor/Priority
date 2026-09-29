"""Claude turns a thread into a structured loop (the text work Jev can't do)."""
from datetime import datetime, timezone

from .llm import ask_json

SYSTEM = """You turn a message thread into one task ("loop") for the user ("Me").
Reply ONLY with JSON:
{"summary": "imperative, under 12 words, e.g. 'Send Sam the pricing deck'",
 "person": "name of the other person",
 "done_when": "observable evidence of completion, e.g. 'Me sends Sam an email with the deck attached'",
 "due": "ISO 8601 datetime if a deadline is stated or clearly implied, else null",
 "cost": integer 1-100 (how bad missing this is: 5 trivial, 30 mild annoyance, 60 real cost, 90 lost opportunity or money),
 "consequence": one of ["money","opportunity","relationship","reputation","legal_health","minor"],
 "reversible": true if a miss can be recovered later,
 "hard_deadline": true if the deadline is set externally and fixed,
 "effort_h": estimated hours of work (0.05 for a quick reply),
 "stakes": "one plain sentence: what happens if this is missed",
 "effort_stated": true only if the text says how long it takes,
 "due_stated": true only if the text gives a time or date}"""


def norm_due(due):
    """Any ISO string (naive = local time) -> UTC ISO string, or None."""
    if not due:
        return None
    try:
        d = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.astimezone()).astimezone(timezone.utc).isoformat()


def extract(thread_state: str, loop_type: str) -> dict:
    # Local time with offset, so "3:30 today" means 3:30 where the user is
    now = datetime.now().astimezone().isoformat(timespec="minutes")
    hint = {"reply": "The other person is waiting for Me to reply or act.",
            "promise": "Me committed to doing or sending something.",
            "waiting": "Me is waiting on the other person to deliver something.",
            "task": "Me noted something Me needs to do."}[loop_type]
    d = ask_json(SYSTEM, f"Current time: {now}\nLoop type: {loop_type}. {hint}\n\nTHREAD:\n{thread_state}")
    d["cost"] = int(max(1, min(100, d.get("cost", 30))))
    d["effort_h"] = float(max(0.02, d.get("effort_h", 0.25)))
    d["due"] = norm_due(d.get("due"))
    return d


import re
from datetime import timedelta

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "five": 5, "ten": 10, "few": 3, "couple": 2}
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_FILLER = re.compile(r"^(ok(ay)?|so|um+|uh+|hey|also|and|i'?d also|i also|i need to|i have to|i must|"
                     r"i should|i want to|i'?ll|remind me to|need to|have to|there is|there's)\b[\s,]*", re.I)


def rule_parse(text: str) -> dict:
    """No-API fallback: understands times, days, urgency words and common task types."""
    s, now = text.lower(), datetime.now().astimezone()
    due, day = None, None
    m = re.search(r"\bin (?:a )?(\d+|a|an|one|two|three|five|ten|few|couple)(?: of)? (minutes?|mins?|hours?|hrs?|days?|weeks?)\b", s)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)[0]]
        due = now + timedelta(**{unit: n})
    if re.search(r"\btomorrow\b", s):
        day = now + timedelta(days=1)
    elif re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", s):
        day = now
    else:
        for i, name in enumerate(_DAYS):
            if re.search(rf"\b{name}\b", s):
                day = now + timedelta(days=((i - now.weekday()) % 7) or 7)
    t = re.search(r"\b(?:at|by|before|for)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", s)
    if due is None and t:
        h, mi, ap = int(t.group(1)), int(t.group(2) or 0), (t.group(3) or "").replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and 1 <= h <= 7:
            h += 12  # "at 3:30" almost always means afternoon
        if h < 24 and mi < 60:
            due = (day or now).replace(hour=h, minute=mi, second=0, microsecond=0)
            if not day and due < now:
                due += timedelta(days=1)
    if due is None and day is not None:
        hour = 20 if "tonight" in s else 18 if day.date() == now.date() else 12
        due = day.replace(hour=hour, minute=0, second=0, microsecond=0)
    prep = bool(re.search(r"\b(prepare|prep|get ready|revise|practi[cs]e)\b", s))
    if due and prep:
        due -= timedelta(minutes=15)  # preparation has to finish before the event

    main = re.split(r"\b(?:but|because|since|although|though|as i)\b", s)[0]
    if re.search(r"not (so |that |very |too )?important|no rush|whenever|someday|low priority|not urgent", s):
        cost = 10
    elif re.search(r"\b(interview|deadline|urgent|asap|exam|assessment|application|important|boss)\b", main):
        cost = 75
    else:
        cost = 30
    kind = ("call" if re.search(r"\b(call|ring|phone)\b", s) else
            "waiting" if re.search(r"waiting on|chase|hear back", s) else
            "reply" if re.search(r"\b(reply|message|text|email|respond|whatsapp)\b", s) else "task")
    effort = {"call": 0.25, "reply": 0.1}.get(kind, 1.5 if prep else 1.0 if re.search(r"\bread", s) else 0.5)
    e = re.search(r"takes?(?: me)?(?: about| around| roughly)? (half an|an|a|one|two|three|\d+(?:\.\d+)?) (hours?|hrs?|minutes?|mins?)", s)
    if e:
        n = {"half an": 0.5, "an": 1, "a": 1, "one": 1, "two": 2, "three": 3}.get(e.group(1))
        n = float(e.group(1)) if n is None else n
        effort = n if e.group(2).startswith("h") else n / 60
    pm = re.search(r"\b(?i:call|ring|phone|message|text|email|reply to|waiting on|waiting for|chase|hear back from)\s+([A-Z][a-z]+)", text)
    person = pm.group(1) if pm and pm.group(1).lower() not in ("him", "her", "them", "back") else ""

    summary = re.split(r"(?<=[.!?])\s", text.strip())[0]
    for _ in range(3):
        summary = _FILLER.sub("", summary).strip()
    summary = (summary[:1].upper() + summary[1:])[:90] if summary else text[:90]
    irreversible = bool(re.search(r"\b(interview|exam|deadline|application|flight)\b", main))
    stakes = ""
    for pat, txt in [(r"interview", "You go into the interview underprepared, and that opportunity may not come back."),
                     (r"exam|assessment|test", "You sit it underprepared and the result sticks."),
                     (r"application|deadline", "The window closes and the opportunity is gone."),
                     (r"flight|train", "You miss it and have to rebook.")]:
        if irreversible and re.search(pat, main):
            stakes = txt
            break
    return {"summary": summary, "person": person, "done_when": "", "cost": cost, "type": kind,
            "effort_stated": bool(e), "due_stated": due is not None,
            "due": due.astimezone(timezone.utc).isoformat() if due else None,
            "consequence": "opportunity" if cost >= 75 else "minor", "reversible": not irreversible,
            "hard_deadline": irreversible, "effort_h": effort, "stakes": stakes}


def capture(text: str) -> dict:
    """Manual capture ("ring Sam back tomorrow"). Uses Claude when a key is set, else rule_parse."""
    rules = rule_parse(text)
    try:
        d = extract(f"Me (note to self): {text}", "task")
        d["type"] = rules["type"]
        d["effort_stated"] = bool(d.get("effort_stated")) or rules["effort_stated"]
        d["due_stated"] = bool(d.get("due_stated")) or rules["due_stated"]
        return d
    except Exception as e:
        print(f"[loops] Claude unavailable, using built-in rules: {e}")
        return rules
