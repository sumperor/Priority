"""Errands: things you go out and do (shopping, pick-ups, the post office, the gym).

The time an errand takes depends on when you go, how you get there and how far it is, so it asks
those instead of "how long will this take?". The minutes are worked out here, never guessed.
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


def questions(text, due_iso, known=None, home=""):
    """Follow-ups for an errand, in the order a person would think about them. Skips what the note said."""
    known = known if known is not None else parse(text)
    qs = []
    now = datetime.now().astimezone()
    day = datetime.fromisoformat(due_iso).astimezone() if due_iso else now
    if not HAS_TIME.search(text or ""):
        label = "today" if day.date() == now.date() else "tomorrow" if day.date() == (now + timedelta(days=1)).date() else f"on {day:%A}"
        opts = []
        for name, h in (("Morning, 9:00", 9), ("Late morning, 11:00", 11), ("Afternoon, 14:00", 14), ("Evening, 18:00", 18)):
            t = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now + timedelta(minutes=15):
                opts.append({"label": name, "value": t.astimezone(timezone.utc).isoformat()})
        if len(opts) < 2:  # late in the day: offer the next few hours instead
            base = (now + timedelta(minutes=30)).replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
            opts = [{"label": f"{t:%H:%M}", "value": t.astimezone(timezone.utc).isoformat()}
                    for t in (base + timedelta(hours=k) for k in range(1, 4))]
        qs.append({"field": "start_at", "question": f"What time {label} do you want to go?", "options": opts})
    if "travel_mode" not in known:
        qs.append({"field": "travel_mode", "question": "How are you getting there?",
                   "options": [{"label": "Walking", "value": "walk"}, {"label": "Cycling", "value": "cycle"},
                               {"label": "Driving", "value": "drive"}, {"label": "Bus or train", "value": "transit"},
                               {"label": "I'll get it delivered", "value": "delivery"}]})
    if known.get("travel_mode") == "delivery":
        return qs
    away = {"travel_mode": "delivery"}
    if "travel_min" not in known and not home:
        qs.append({"field": "home", "kind": "text", "question": "Where are you setting off from? I'll remember it.",
                   "placeholder": "e.g. your postcode", "skip_if": away})
    qs.append({"field": "place", "kind": "text", "question": "Where are you going?", "placeholder": "e.g. Tesco on the high street",
               "skip_if": away})
    if "base_h" not in known:
        qs.append({"field": "there_min", "question": "How long will you be there?",
                   "options": [{"label": f"{m} min", "value": m} for m in (10, 15, 30, 45, 60)], "skip_if": away})
    if "travel_min" not in known:
        # only asked if the route lookup couldn't work it out
        qs.append({"field": "travel_min", "question": "I couldn't look up the route. How long does it take to get there, one way?",
                   "options": [{"label": f"{m} min", "value": m} for m in (5, 10, 20, 30, 45)],
                   "skip_if": away, "skip_if_set": "travel_min"})
    return qs


def recompute(loop):
    """Time needed and deadline from the answers. Returns the fields to update."""
    base = loop.get("base_h") or loop.get("effort_h") or 0.5
    mode, mins = loop.get("travel_mode"), loop.get("travel_min")
    effort = 0.25 if mode == "delivery" else base + 2 * (mins or 0) / 60
    f = {"effort_h": round(effort, 3)}
    if loop.get("commit_at"):
        start = datetime.fromisoformat(loop["commit_at"])
        # due is set so the plan's "start by" lands exactly on the time you chose
        f["due"] = (start + timedelta(hours=effort * 1.25 + 0.25)).isoformat()
    if mode == "delivery":
        f["note"] = "Getting it delivered"
    elif mode:
        from .forecast import dur
        bits = [MODES.get(mode, mode)]
        if loop.get("place"):
            bits.append(f"to {loop['place']}")
        note = "Going " + " ".join(bits)
        if mins:
            note += f". {mins} min each way + {round(base * 60)} min there = {dur(effort)}"
        f["note"] = note
    return f
