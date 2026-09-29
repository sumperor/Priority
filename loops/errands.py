"""Errands: things you go out and do (shopping, pick-ups, the post office, the gym).

One question at a time, and only what the note didn't say:
  1. which day (a calendar of the next two weeks, showing how busy each day is)
  2. what time that day (only times your calendar is free)
  3. where: taken from the note ("from Halfords in Farnborough") or asked, then looked up and shown
     on a map, with the travel time for walking, cycling and driving as the choices
  4. how long you'll be there, if the note didn't say
  5. the estimate: when to leave, when you'll be back, and the sum that got there
Every minute is worked out here or by the route lookup, never guessed.
"""
import re
from datetime import datetime, timedelta, timezone

ERRAND = re.compile(r"\b(buy|shop(ping)?|groceries|grocery|supermarket|pick up|collect|drop off|post (a|the|my)|post office|"
                    r"return .{1,30} to|pharmacy|chemist|haircut|barber|gym|get .{1,20} from|go to (the )?(shop|store|bank|library))\b", re.I)
HAS_TIME = re.compile(r"\b\d{1,2}([:.]\d{2})?\s*(am|pm)\b|\b(at|by|before|around) \d{1,2}([:.]\d{2})?\b|\b(noon|midday)\b", re.I)
MODES = {"walk": "walking", "cycle": "cycling", "drive": "driving", "transit": "bus or train", "delivery": "delivery"}
_N = r"(\d+|half an|an|a|one|two)\s*(hours?|hrs?|h|minutes?|mins?|m)\b"
EACH_WAY = re.compile(_N + r"\s*(each way|either way|there|one way)", re.I)
THERE = re.compile(_N + r"\s*(for|of|in|at)\s+(the\s+)?(shopping|shop|store|supermarket|there|it)\b|"
                   r"(shopping|shop)\s+(takes?|for|is)\s+(about\s+)?" + _N, re.I)
MODE_WORDS = [("walk", r"\bwalk(ing)?\b|on foot"), ("cycle", r"\b(cycl(e|ing)|bike|biking)\b"),
              ("drive", r"\b(driv(e|ing)|by car)\b"), ("transit", r"\b(bus|train|tube|metro)\b"),
              ("delivery", r"\bdeliver(y|ed)\b")]
PLACE = re.compile(r"\b(?:from|at|in|to)\s+((?:the\s+)?[A-Z][\w'\u2019&.-]*(?:\s+(?:[A-Z][\w'\u2019&.-]*|in|on|at|of|&))*)")
NOT_PLACE = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tonight",
             "tomorrow", "the", "noon", "midday", "i"}
DAY_START, DAY_END = 8, 21   # latest time to set off


def _min(n, unit):
    v = {"half an": 0.5, "an": 1, "a": 1, "one": 1, "two": 2}.get(n.lower())
    v = float(n) if v is None else v
    return round(v * 60) if unit.lower().startswith("h") else round(v)


def parse(text):
    """What the note already says: how you're going, minutes each way, minutes there."""
    t, out = text or "", {}
    for mode, rx in MODE_WORDS:
        if re.search(rx, t, re.I):
            out["travel_mode"] = mode
            break
    m = EACH_WAY.search(t)
    if m:
        out["travel_min"] = _min(m.group(1), m.group(2))
    m = THERE.search(t)
    if m:
        n, u = (m.group(1), m.group(2)) if m.group(1) else (m.group(10), m.group(11))
        out["base_h"] = round(_min(n, u) / 60, 3)
    return out


def is_errand(text):
    return bool(ERRAND.search(text or ""))


def _min(n, unit):
    v = {"half an": 0.5, "an": 1, "a": 1, "one": 1, "two": 2}.get(n.lower())
    v = float(n) if v is None else v
    return round(v * 60) if unit.lower().startswith("h") else round(v)


def place_from(text):
    """'Buy skating shoes from Halfords in Farnborough' -> 'Halfords in Farnborough'."""
    for m in PLACE.finditer(text or ""):
        words = []
        for w in m.group(1).split():
            if w.lower().strip(".,") in NOT_PLACE and words:
                break          # "Tesco Express on Monday" stops before the day
            words.append(w)
        p = re.sub(r"(\s+(in|on|at|of|&))+$", "", " ".join(words)).strip(" .,")
        first = re.sub(r"^the\s+", "", p, flags=re.I).split(" ")[0].lower() if p else ""
        if p and first not in NOT_PLACE:
            return p[:80]
    return ""


def parse(text):
    """What the note already says: where, how you're going, minutes each way, minutes there."""
    t, out = text or "", {}
    for mode, rx in MODE_WORDS:
        if re.search(rx, t, re.I):
            out["travel_mode"] = mode
            break
    m = EACH_WAY.search(t)
    if m:
        out["travel_min"] = _min(m.group(1), m.group(2))
    m = THERE.search(t)
    if m:
        n, u = (m.group(1), m.group(2)) if m.group(1) else (m.group(10), m.group(11))
        out["base_h"] = round(_min(n, u) / 60, 3)
    place = place_from(t)
    if place:
        out["place"] = place
    return out


def _busy(events):
    out = []
    for e in events or []:
        try:
            s = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
            en = datetime.fromisoformat((e.get("end") or e["start"]).replace("Z", "+00:00"))
            out.append((s, en, e.get("title", "")))
        except (KeyError, ValueError):
            continue
    return out


def _free_starts(day, busy, need, now):
    """Set-off times on `day` (local) when the whole trip fits between calendar events."""
    out = []
    t = datetime.combine(day, datetime.min.time()).astimezone().replace(hour=DAY_START)
    end = t.replace(hour=DAY_END)
    while t <= end:
        if t >= now + timedelta(minutes=15) and all(not (t < b and t + need > a) for a, b, _ in busy):
            out.append(t)
        t += timedelta(minutes=30)
    return out


def _calendar(busy, need, now):
    today = now.astimezone().date()
    days = []
    for k in range(14):
        d = today + timedelta(days=k)
        n = sum(1 for a, b, _ in busy if a.astimezone().date() == d)
        free = _free_starts(d, busy, need, now)
        if not free:
            if k == 0:
                continue   # nothing left today
            sub = "No free time"
        else:
            sub = "Free all day" if n == 0 else f"{n} event{'s' if n > 1 else ''}"
        label = "Today" if k == 0 else "Tomorrow" if k == 1 else f"{d:%a %d %b}"
        days.append({"value": d.isoformat(), "label": label, "sub": sub, "off": not free})
    return days


def _times(day, busy, need, now):
    starts = _free_starts(day, busy, need, now)
    if len(starts) > 10:   # every half hour is too many buttons: keep the hours, plus the first free slot
        starts = [starts[0]] + [t for t in starts[1:] if t.minute == 0]
    return [{"label": f"{t:%H:%M}", "value": t.astimezone(timezone.utc).isoformat()} for t in starts[:14]]


def trip_h(loop):
    """Hours for the whole trip: there and back plus the time there."""
    if loop.get("travel_mode") == "delivery":
        return 0.25
    base = loop.get("base_h") if loop.get("base_h") is not None else 0.5
    return base + 2 * (loop.get("travel_min") or 0) / 60


def next_step(loop, start, events, now=None, lookup=None):
    """The one question to ask next for this errand, or the finished estimate. `start` is where you set
    off from (coordinates or a place); `lookup(place, mode, start)` is maps.travel."""
    from .forecast import dur
    now = now or datetime.now(timezone.utc)
    busy = _busy(events)
    need = timedelta(hours=max(trip_h(loop), 0.5))
    ctx = loop["summary"]
    if not loop.get("commit_at"):
        if not loop.get("errand_day"):
            return {"field": "errand_day", "kind": "calendar", "ctx": ctx, "question": "Which day do you want to go?",
                    "options": _calendar(busy, need, now)}
        day = datetime.fromisoformat(loop["errand_day"]).date()
        opts = _times(day, busy, need, now)
        label = "today" if day == now.astimezone().date() else f"on {day:%A %d %B}"
        q = f"What time {label}? These are the times you're free." if opts else f"You're not free {label}. Pick another day."
        return {"field": "start_at", "kind": "choice", "ctx": ctx, "question": q,
                "options": opts + [{"label": "Another day", "value": "__day"}]}
    mode = loop.get("travel_mode")
    if mode != "delivery":
        place = loop.get("place")
        if not place:
            return {"field": "place", "kind": "text", "ctx": ctx, "question": "Where are you going?",
                    "placeholder": "e.g. Halfords, Farnborough", "extra": [{"label": "I'll get it delivered", "value": "delivery"}]}
        if not loop.get("place_ok"):
            if not start:
                return {"field": "home", "kind": "text", "ctx": ctx, "locate": True,
                        "question": "Where are you setting off from? A street, station or landmark is fine.",
                        "placeholder": "e.g. Farnborough Main station"}
            routes = {m: lookup(place, m, start) for m in ("walk", "cycle", "drive")} if lookup else {}
            found = next((r for r in routes.values() if r), None)
            opts = []
            for m, label in (("walk", "Walk"), ("cycle", "Cycle"), ("drive", "Drive")):
                r = routes.get(m)
                opts.append({"label": label, "sub": f"{r['minutes']} min each way" if r else "", "value": m})
            opts += [{"label": "Bus or train", "sub": "you tell me how long", "value": "transit"},
                     {"label": "Get it delivered", "value": "delivery"}, {"label": "Wrong place", "value": "__wrong"}]
            if found:
                q = f"Found it: {found.get('to') or place}, {found['km']} km away. How are you getting there?"
                return {"field": "travel_mode", "kind": "place", "ctx": ctx, "question": q, "options": opts,
                        "map": {k: found[k] for k in ("lat", "lon") if k in found}}
            from .maps import last
            why = last.get("error") or "no match"
            return {"field": "travel_mode", "kind": "choice", "ctx": ctx, "options": opts,
                    "question": f"I couldn't find \"{place}\" on the map ({why}). How are you getting there?"}
        if loop.get("travel_min") is None:
            return {"field": "travel_min", "kind": "choice", "ctx": ctx, "question": "How long does it take to get there, one way?",
                    "options": [{"label": f"{m} min", "value": m} for m in (5, 10, 15, 20, 30, 45, 60)]}
        if loop.get("base_h") is None:
            where = (place or "there").split(",")[0].split(" in ")[0]
            return {"field": "there_min", "kind": "choice", "ctx": ctx, "question": f"How long will you be in {where}?",
                    "options": [{"label": f"{m} min", "value": m} for m in (10, 15, 20, 30, 45, 60)]}
    return {"kind": "summary", "ctx": ctx, "question": "Here's the plan", "lines": estimate(loop)}


def estimate(loop):
    from .forecast import dur
    total = trip_h(loop)
    leave = datetime.fromisoformat(loop["commit_at"]).astimezone()
    back = leave + timedelta(hours=total)
    if loop.get("travel_mode") == "delivery":
        return [f"Order it at {leave:%H:%M} on {leave:%A}. About 15 min."]
    there = round((loop.get("base_h") if loop.get("base_h") is not None else 0.5) * 60)
    way = loop.get("travel_min") or 0
    how = MODES.get(loop.get("travel_mode") or "", "travelling")
    return [f"Leave at {leave:%H:%M} on {leave:%A %d %B}, back about {back:%H:%M}.",
            f"{way} min {how} each way + {there} min there = {dur(total)}."]


def recompute(loop):
    """Time needed and deadline from the answers. Returns the fields to update."""
    from .forecast import dur
    mode, mins = loop.get("travel_mode"), loop.get("travel_min")
    effort = trip_h(loop)
    base = loop.get("base_h") if loop.get("base_h") is not None else 0.5
    f = {"effort_h": round(effort, 3)}
    if loop.get("commit_at"):
        start = datetime.fromisoformat(loop["commit_at"])
        # due is set so the plan's "start by" lands exactly on the time you chose
        f["due"] = (start + timedelta(hours=effort * 1.25 + 0.25)).isoformat()
    if mode == "delivery":
        f["note"] = "Getting it delivered"
    elif mode or loop.get("place"):
        note = "Going " + " ".join(x for x in (MODES.get(mode or "", ""), f"to {loop['place']}" if loop.get("place") else "") if x)
        if mins:
            note += f". {mins} min each way + {round(base * 60)} min there = {dur(effort)}"
        f["note"] = note
    return f
