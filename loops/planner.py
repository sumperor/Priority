"""The calendar: your events, your meetings, your deadlines, and Sparrow's plan for your tasks.

Tasks are placed into free time, most important first, finishing before each one's latest safe start
where possible. A time you chose (or dragged a block to) is kept exactly. All times are worked out here.
"""
import time
from datetime import datetime, timedelta, timezone

DAY_START, DAY_END = 8, 22          # plan work between 08:00 and 22:00 local
STEP = timedelta(minutes=15)
_cache = {}                          # calendar events per range, for a few minutes


def _utc(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.astimezone()).astimezone(timezone.utc)


def _round_up(t):
    m = (t.minute // 15 + (1 if t.minute % 15 or t.second else 0)) * 15
    return t.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=m)


def calendar_events(start, end):
    """Events from connected calendars (Google, Outlook). Cached for 5 minutes; empty if none connected."""
    from .connect import connected
    from .connectors import ALL
    key = (start.date().isoformat(), (end - start).days)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    out = []
    for name in connected():
        c = ALL.get(name)
        if c and hasattr(c, "events"):
            try:
                for e in c().events(start, end):
                    out.append({"title": e["title"], "start": _utc(e["start"]).isoformat(),
                                "end": _utc(e.get("end") or e["start"]).isoformat(), "source": e.get("source", name)})
            except Exception as ex:
                print(f"[sparrow] calendar ({name}) unavailable: {ex}")
    _cache[key] = (time.time(), out)
    return out


def _free(busy, t, dur):
    return all(not (t < b_end and t + dur > b_start) for b_start, b_end in busy)


def _next_slot(busy, t, dur, until):
    """Earliest start >= t inside working hours that doesn't overlap anything, or None before `until`."""
    t = _round_up(t)
    while t < until:
        local = t.astimezone()
        day_start = local.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
        day_end = local.replace(hour=DAY_END, minute=0, second=0, microsecond=0)
        if local < day_start:
            t = day_start.astimezone(timezone.utc)
            continue
        if local + dur > day_end:
            t = (day_start + timedelta(days=1)).astimezone(timezone.utc)
            continue
        if _free(busy, t, dur):
            return t
        t += STEP
    return None


def build(rows, start, days, events=None, now=None):
    """rows: ranked loops (plan order). Returns {"events", "meetings", "deadlines", "blocks"} for the range."""
    now = now or datetime.now(timezone.utc)
    end = start + timedelta(days=days)
    events = calendar_events(start, end) if events is None else events
    busy = [(_utc(e["start"]), _utc(e["end"])) for e in events]
    meetings, deadlines, blocks = [], [], []

    for r in rows:
        if r["status"] != "open":
            continue
        if r["type"] == "meeting":
            s = _utc(r["due"])
            m = {"id": r["id"], "title": r["summary"], "start": s.isoformat(), "end": (s + timedelta(minutes=30)).isoformat()}
            meetings.append(m)
            busy.append((s, s + timedelta(minutes=30)))
        elif r["type"] != "waiting" and not r.get("past_deadline") and not r.get("due_guess"):
            deadlines.append({"id": r["id"], "title": r["summary"], "at": r["due"], "level": r["forecast"]["level"]})

    # Fixed first: times you chose or dragged to
    todo = [r for r in rows if r["status"] == "open" and r["type"] not in ("waiting", "meeting") and not r.get("past_deadline")]
    for r in todo:
        if r.get("commit_at"):
            s = _utc(r["commit_at"])
            dur = max(STEP, timedelta(hours=r["effort_adj"]))
            busy.append((s, s + dur))
            blocks.append(_block(r, s, dur, fixed=True))
    # Then the rest: earliest real deadline first (the order that misses the fewest), ties by value;
    # tasks with no deadline fill the gaps after
    order = sorted(todo, key=lambda r: (bool(r.get("due_guess")), _utc(r["forecast"]["latest_start"]),
                                        -(r.get("ev_per_hour") or 0)))
    for r in order:
        if r.get("commit_at"):
            continue
        dur = max(STEP, timedelta(minutes=round(r["effort_adj"] * 60 / 15) * 15))
        latest = _utc(r["forecast"]["latest_start"])
        s = _next_slot(busy, max(now, start), dur, latest + STEP) or _next_slot(busy, max(now, start), dur, end + timedelta(days=14))
        if s is None:
            continue
        busy.append((s, s + dur))
        blocks.append(_block(r, s, dur, fixed=False, late=s > latest))

    within = lambda a, b: _utc(b) > start and _utc(a) < end
    return {"events": [e for e in events if within(e["start"], e["end"])],
            "meetings": [m for m in meetings if within(m["start"], m["end"])],
            "deadlines": [d for d in deadlines if start <= _utc(d["at"]) < end],
            "blocks": [b for b in blocks if within(b["start"], b["end"])],
            "unplaced": [r["id"] for r in todo if not any(b["id"] == r["id"] for b in blocks)],
            "day_start": DAY_START, "day_end": DAY_END}


def _block(r, s, dur, fixed, late=False):
    return {"id": r["id"], "title": r["summary"], "area": r.get("area") or "Other", "start": s.isoformat(),
            "end": (s + dur).isoformat(), "fixed": fixed, "late": late, "level": r["forecast"]["level"],
            "minutes": int(dur.total_seconds() // 60)}


def move_warning(loop, new_start, effort_h):
    """Plain words if the new time is too late, from the loop's own deadline and effort."""
    due = _utc(loop["due"])
    end = new_start + timedelta(hours=effort_h)
    if end <= due:
        return ""
    short = round((end - due).total_seconds() / 60)
    return (f"That finishes after the deadline ({due.astimezone():%a %H:%M}). You'd be {short} min short." if new_start < due
            else f"That's after the deadline ({due.astimezone():%a %H:%M}).")
