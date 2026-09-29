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


def is_errand(text):
    return bool(ERRAND.search(text or ""))


def questions(text, due_iso):
    """Follow-ups for an errand, in the order a person would think about them."""
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
    qs.append({"field": "travel_mode", "question": "How are you getting there?",
               "options": [{"label": "Walking", "value": "walk"}, {"label": "Cycling", "value": "cycle"},
                           {"label": "Driving", "value": "drive"}, {"label": "Bus or train", "value": "transit"},
                           {"label": "I'll get it delivered", "value": "delivery"}]})
    qs.append({"field": "travel_min", "question": "How long does it take to get there, one way?",
               "options": [{"label": f"{m} min", "value": m} for m in (5, 10, 20, 30, 45)],
               "skip_if": {"travel_mode": "delivery"}})
    qs.append({"field": "place", "kind": "text", "question": "Where are you going?", "placeholder": "e.g. Tesco on the high street",
               "skip_if": {"travel_mode": "delivery"}})
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
    bits = []
    if mode:
        bits.append(MODES.get(mode, mode) + (f", {mins} min each way" if mins and mode != "delivery" else ""))
    if loop.get("place"):
        bits.append(f"to {loop['place']}")
    if bits:
        f["note"] = "Going " + " ".join(bits) if mode != "delivery" else "Getting it delivered"
    return f
