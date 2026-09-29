"""Calendar invitations arriving by email (Google Calendar, Outlook, Teams, Zoom).

An invite is a meeting at a fixed time, not a quick reply. This pulls out the title, when it is,
who organised it and how to join, and strips the email's boilerplate so previews show the meeting.
"""
import re
from datetime import datetime, timedelta, timezone

SUBJECT = re.compile(r"^(?:updated\s+)?invitation(?:\s+updated)?\s*:\s*(.+?)\s*@\s*(.+?)(?:\s*\(([^()]*@[^()]*)\))?\s*$", re.I)
BODY_HINT = re.compile(r"(calendar\.google\.com/calendar/event|invitation from google calendar|join with google meet|"
                       r"microsoft teams meeting|join the meeting now|zoom\.us/j/|\.ics\b)", re.I)
BOILER = re.compile(r"(this event isn't in your calendar yet|you haven't interacted with|do you want to automatically add|"
                    r"^add to calendar|invitation from google calendar|you are receiving this|forwarding this invitation|"
                    r"learn more|^reply for |view all guest info|^rsvp|more options|^invitation$|google calendar\b.*notifications|"
                    r"to stop receiving|^https?://\S+$)", re.I)
CUT = re.compile(r"(invitation from google calendar|^reply for |view all guest info|you are receiving this)", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
TZ = {"BST": 1, "GMT": 0, "UTC": 0, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "EST": -5, "EDT": -4, "CST": -6,
      "CDT": -5, "PST": -8, "PDT": -7, "IST": 5.5, "SGT": 8, "AEST": 10, "AEDT": 11}
WHEN = re.compile(r"(?:(?P<d1>\d{1,2})\s+(?P<m1>[A-Za-z]{3,9})|(?P<m2>[A-Za-z]{3,9})\s+(?P<d2>\d{1,2}),?)\s+(?P<y>\d{4})"
                  r"\s*(?:⋅|,)?\s*(?P<t1>\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*(?:[-–]|to)\s*(?P<t2>\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)?"
                  r"(?:\s*\((?P<tz>[A-Z]{2,5})\))?", re.I)
GENERIC = re.compile(r"^(meeting|call|chat|catch[- ]?up|sync|quick (call|chat)|intro(duction)?|coffee|1:1|one to one|untitled|event)"
                     r"(\s+with\s+.+)?$", re.I)


def is_invite(m):
    return bool(SUBJECT.match(m.subject or "")) or bool(BODY_HINT.search(m.text or "") and
                                                        re.search(r"\b(invit|meeting|event)", f"{m.subject} {m.text[:400]}", re.I))


def _time(t, ampm_hint=""):
    t = t.strip().lower().replace(".", ":")
    ap = "pm" if t.endswith("pm") else "am" if t.endswith("am") else ampm_hint
    t = t.rstrip("apm ").strip()
    h, _, mi = t.partition(":")
    h, mi = int(h), int(mi or 0)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, mi, ap


def when(text):
    """(start, end) as UTC datetimes from 'Wed 1 Oct 2025 3pm - 3:30pm (BST)' style text, or (None, None)."""
    m = WHEN.search(text or "")
    if not m:
        return None, None
    mon = MONTHS.get((m["m1"] or m["m2"] or "")[:3].lower())
    if not mon:
        return None, None
    day, year = int(m["d1"] or m["d2"]), int(m["y"])
    h1, mi1, ap1 = _time(m["t1"])
    if m["t2"]:
        h2, mi2, ap2 = _time(m["t2"])
        if not ap1 and ap2 and ap2 == "pm" and h1 < 12 and h1 + 12 <= h2:
            h1 += 12  # "3 - 3:30pm"
    tz = TZ.get((m["tz"] or "").upper())
    zone = timezone(timedelta(hours=tz)) if tz is not None else datetime.now().astimezone().tzinfo
    try:
        start = datetime(year, mon, day, h1, mi1, tzinfo=zone)
        end = datetime(year, mon, day, h2, mi2, tzinfo=zone) if m["t2"] else start + timedelta(minutes=30)
    except ValueError:
        return None, None
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def clean(text):
    """Drop calendar boilerplate so a preview shows what the email is actually about."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if CUT.search(s):
            break
        if s and not BOILER.search(s):
            out.append(s)
    return "\n".join(out)


def parse(m):
    sm = SUBJECT.match(m.subject or "")
    title = (sm.group(1) if sm else re.sub(r"^(updated\s+)?invitation\s*:\s*", "", m.subject or "", flags=re.I)).strip()
    start, end = when(sm.group(2) if sm else "")
    if not start:
        start, end = when(m.text)
    body = clean(m.text)
    org = re.search(r"(?:organi[sz]er|organi[sz]ed by)\s*\n?\s*([^\n]+)", body, re.I)
    organizer = (org.group(1).strip() if org else m.sender_name or m.sender).split("<")[0].strip()
    join = "Google Meet" if "meet.google.com" in (m.text or "") else "Teams" if "teams.microsoft.com" in (m.text or "") \
        else "Zoom" if "zoom.us" in (m.text or "") else ""
    lines = [l for l in body.splitlines() if l != title and not WHEN.search(l) and not re.match(
        r"^(join|organi[sz]er|guests?|view|meeting link|join by phone|more phone numbers|\+?\d[\d\s-]{6,}|pin:|time zone|"
        r"united kingdom time|reply|yes|no|maybe)\b", l, re.I) and l != organizer and "@" not in l]
    return {"title": title or "Meeting", "start": start, "end": end, "organizer": organizer, "join": join,
            "generic": bool(GENERIC.match(title or "")), "notes": " ".join(lines)[:300]}


def summary(inv):
    who = inv["organizer"]
    if inv["generic"] or not inv["title"]:
        return f"Meeting with {who}, topic not given"
    return f"Meeting with {who}: {inv['title']}"[:120]


def preview(inv):
    bits = [inv["title"]]
    if inv["start"]:
        s, e = inv["start"].astimezone(), inv["end"].astimezone()
        bits.append(f"{s:%a %d %b, %H:%M} to {e:%H:%M}")
    if inv["join"]:
        bits.append(f"on {inv['join']}")
    text = ", ".join(bits) + f". Organised by {inv['organizer']}."
    return text + (f" {inv['notes']}" if inv["notes"] else " No agenda in the invite.")
