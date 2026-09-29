"""Cost-of-miss forecasting: what happens if this slips, and what else it drags with it."""
from datetime import datetime, timedelta, timezone

MISS_FALLBACK = {
    "reply": "The thread goes cold and they move on or ask someone else.",
    "promise": "You break your word, and the next ask gets harder.",
    "waiting": "Your own plans stall until they respond.",
    "call": "They may read the silence as you ignoring them.",
    "task": "It rolls into tomorrow and stacks up.",
    "assessment": "If it isn't done by the deadline, the application usually ends there.",
}


def dur(h):
    """Hours as people say them: "55 min", "1 h 10 min", "2 h"."""
    m = max(1, round((h or 0) * 60))
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h" + (f" {m % 60} min" if m % 60 else "")


def _hrs(h):
    h = abs(h)
    return f"{max(1, round(h))}h" if h < 48 else f"{round(h / 24)}d"


def knock_on(loop, open_by_id):
    """Follow the 'blocks' chain: missing A delays B, which delays C."""
    out, seen, cur = [], {loop["id"]}, loop
    while cur.get("blocks") and cur["blocks"] not in seen and cur["blocks"] in open_by_id:
        cur = open_by_id[cur["blocks"]]
        out.append(cur)
        seen.add(cur["id"])
    return out


def forecast(loop, p_miss, effort_h, cost, open_by_id):
    now = datetime.now(timezone.utc)
    due = datetime.fromisoformat(loop["due"])
    hours_left = (due - now).total_seconds() / 3600
    # Latest safe start: effort plus a 25% overrun buffer plus 15 minutes
    latest_start = due - timedelta(hours=effort_h * 1.25 + 0.25)
    chain = knock_on(loop, open_by_id)
    stakes = loop.get("stakes") or MISS_FALLBACK.get(loop["type"], MISS_FALLBACK["task"])

    if hours_left < 0:
        headline, level = f"Missed by {_hrs(hours_left)}. {stakes}", "late"
    elif latest_start <= now:
        headline, level = f"At risk: needs about {dur(effort_h)} and only {_hrs(hours_left)} left.", "late"
    elif hours_left < 24:
        ls = latest_start.astimezone()
        when = f"{ls:%H:%M}" if ls.date() == now.astimezone().date() else f"tomorrow {ls:%H:%M}"
        headline, level = f"Start by {when} to finish safely.", "soon"
    else:
        headline, level = f"Safe if started by {latest_start.astimezone():%a %H:%M}.", "ok"

    hard, reversible = bool(loop.get("hard_deadline")), bool(loop.get("reversible", 1))
    if hard and not reversible:
        slip = "A 1-day slip past the deadline means it's lost for good."
    elif hard:
        slip = "A 1-day slip makes it late; recoverable, but it will cost you credibility."
    else:
        slip = "Soft deadline: a 1-day slip costs little on its own, but each delay makes a miss more likely."

    if_missed = stakes
    if chain:
        if_missed += " It also holds up: " + "; ".join(c["summary"] for c in chain) + "."

    return {"headline": headline, "level": level, "if_missed": if_missed, "slip_1d": slip,
            "knock_on": [{"id": c["id"], "summary": c["summary"]} for c in chain],
            "latest_start": latest_start.isoformat(), "hours_left": round(hours_left, 1),
            "p_miss": round(p_miss, 2), "expected_cost": round(p_miss * cost, 1)}
