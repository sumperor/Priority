"""Calendar invitations arriving by email (Google Calendar, Outlook, Teams, Zoom).

An invite is a meeting at a fixed time, not a quick reply. This pulls out the title, when it is,
who organised it and how to join, and strips the email's boilerplate so previews show the meeting.
"""
import re
from datetime import datetime, timedelta, timezone

PREFIX = r"^(?:updated\s+|new\s+)?invitation(?:\s+updated)?(?:\s+from\s+an?\s+unknown\s+sender)?\s*:\s*"
SUBJECT = re.compile(PREFIX + r"(.+?)\s*@\s*(.+?)(?:\s*\(([^()]*@[^()]*)\))?\s*$", re.I)
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
    title = (sm.group(1) if sm else re.sub(PREFIX, "", m.subject or "", flags=re.I)).strip()
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
    t = inv["title"] if len(inv["title"]) <= 60 else inv["title"][:60].rsplit(" ", 1)[0] + "\u2026"
    return f"Meeting with {who}: {t}"


def preview(inv):
    bits = [inv["title"]]
    if inv["start"]:
        s, e = inv["start"].astimezone(), inv["end"].astimezone()
        bits.append(f"{s:%a %d %b, %H:%M} to {e:%H:%M}")
    if inv["join"]:
        bits.append(f"on {inv['join']}")
    text = ", ".join(bits) + f". Organised by {inv['organizer']}."
    return text + (f" {inv['notes']}" if inv["notes"] else " No agenda in the invite.")


# ---------------------------------------------------------------- bookings and meeting requests
# Confirmations of something at a fixed time: event bookings, tickets, appointments, interviews, webinars
BOOKED = re.compile(
    r"\b(booking|reservation|appointment|registration|place|spot|seat|ticket|tickets|interview|meeting|call|webinar|"
    r"session|event|class|viewing|consultation|tour|visit)s?\b[^.\n]{0,40}\b(is |has been |are )?(confirmed|scheduled|booked|"
    r"set|reserved|registered)\b|"
    r"^(confirmed|booking confirmed|event scheduled|new event|you'?re (registered|booked|going|in|confirmed)|"
    r"your (tickets?|booking|registration|appointment)|see you)\b|"
    r"\b(you'?re (registered|booked|going|all set|confirmed) for|thanks for (registering|booking)|we look forward to seeing you|"
    r"has been added to your calendar|add (it|this) to your calendar)\b", re.I | re.M)
# Someone asking to meet at a time: "can we meet Thursday at 3?", "are you free for a call tomorrow at 10?"
ASK_MEET = re.compile(r"\b((can|could|shall|should|would) (we|you|i) (meet|chat|talk|speak|call|catch up|hop on|grab)|"
                      r"(are|would) you (be )?(free|available)|does .{1,25} (work|suit) (for )?you|how about|let'?s (meet|chat|talk|catch up))\b", re.I)
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DAY = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*"
_T = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?"
DATE_TIME = re.compile(
    r"(?:(?P<wd>" + _DAY + r")\.?,?\s+)?(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>" + _MON + r")|(?P<m2>" + _MON +
    r")\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?|(?P<dd>\d{1,2})/(?P<mm>\d{1,2}))(?:,?\s+(?P<y>\d{4}))?"
    r"|(?P<rel>today|tonight|tomorrow)|(?:(?:on|this|next)\s+)?(?P<wd2>" + _DAY + r")\b", re.I)
TIME = re.compile(r"^[\s,⋅|-]*(?:(?:start\s+)?time\s*:\s*)?(?P<at>at|from|@)?\s*" + _T + r"(?:\s*(?:-|–|to|until)\s*" + _T + r")?", re.I)
TIME_BEFORE = re.compile(_T + r"\s*(?:on|,)?\s*$", re.I)


def when_loose(text, now=None):
    """(start, end) in UTC from the ways people write it: 'Thursday 2 October at 2pm', 'Thu 2 Oct, 14:00-15:00',
    'October 2 at 10am', '02/10 14:00', 'tomorrow at 10', 'on Thursday at 3pm'. The next such time from now,
    in your local time zone. (None, None) if there's no clear date and time."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    for m in DATE_TIME.finditer(text or ""):
        tm = TIME.match(text[m.end():m.end() + 30])
        if not tm or not (tm.group(3) or tm.group(4) or tm.group("at")):
            before = TIME_BEFORE.search(text[max(0, m.start() - 16):m.start()])   # "at 3pm on Thursday"
            if not before or not (before.group(2) or before.group(3)):
                continue
            h, mi, ap, h2, mi2, ap2 = before.group(1), before.group(2), before.group(3), None, None, None
        else:
            h, mi, ap, h2, mi2, ap2 = tm.group(2), tm.group(3), tm.group(4), tm.group(5), tm.group(6), tm.group(7)
        h, mi = int(h), int(mi or 0)
        if h > 23 or mi > 59:
            continue
        if not ap and ap2 == "pm" and h < 12:
            ap = "pm"
        if ap and ap.lower() == "pm" and h < 12:
            h += 12
        if ap and ap.lower() == "am" and h == 12:
            h = 0
        if not ap and h < 7:
            h += 12          # "at 3" means 3pm
        if m["rel"]:
            day = now.date() + timedelta(days=1 if m["rel"].lower() == "tomorrow" else 0)
        elif m["wd2"] and not (m["d1"] or m["d2"] or m["dd"]):
            want = [d[:3] for d in DAYS].index(m["wd2"][:3].lower())
            day = now.date() + timedelta(days=(want - now.weekday()) % 7)
            if day == now.date() and now.hour >= h:
                day += timedelta(days=7)
        else:
            mon = MONTHS.get((m["m1"] or m["m2"] or "")[:3].lower()) if not m["dd"] else int(m["mm"])
            d = int(m["d1"] or m["d2"] or m["dd"])
            try:
                y = int(m["y"]) if m["y"] else now.year
                day = datetime(y, mon, d).date()
                if not m["y"] and day < now.date() - timedelta(days=1):
                    day = datetime(y + 1, mon, d).date()
            except (ValueError, TypeError):
                continue
        start = datetime(day.year, day.month, day.day, h, mi, tzinfo=now.tzinfo)
        end = start + timedelta(minutes=60)
        if h2:
            eh, em = int(h2), int(mi2 or 0)
            if (ap2 or "").lower() == "pm" and eh < 12:
                eh += 12
            if eh <= 23 and em <= 59:
                e = start.replace(hour=eh, minute=em)
                end = e if e > start else e + timedelta(hours=12) if eh < 12 else start + timedelta(minutes=60)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    return None, None


def is_booking(m):
    return bool(BOOKED.search(f"{m.subject}\n{(m.text or '')[:1500]}"))


def asks_to_meet(m):
    return bool(ASK_MEET.search(f"{m.subject}\n{(m.text or '')[:1500]}"))


def booking_title(m):
    """'Booking confirmed: Careers Fair 2025' -> 'Careers Fair 2025'."""
    t = re.sub(r"^((re|fwd?):\s*)+", "", m.subject or "", flags=re.I)
    t = re.sub(r"^(confirmed|booking confirmed|registration confirmed|event scheduled|new event|reminder|you'?re (registered|booked|going|in|confirmed)( for)?|"
               r"your (booking|tickets?|registration|appointment)( for| is confirmed| confirmation)?)\s*[:\-!,]*\s*", "", t, flags=re.I)
    t = re.sub(r"\s*(is|has been)\s+(confirmed|booked|scheduled)\.?$", "", t, flags=re.I).strip(" :-!")
    return t or (m.subject or "Booking")
