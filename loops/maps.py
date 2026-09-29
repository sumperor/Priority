"""Travel time from A to B, looked up instead of asked.

Where you are: your browser's location ("here", sent by the page), or a place you typed once ("home").
Where you're going: searched on OpenStreetMap (Nominatim), picking the match nearest to you, so
"Currys" finds your local branch. With a Claude key, a loose or misheard name ("Curry's Vonbra")
is first web-searched into a real address; without one the OpenStreetMap search still runs.
The route itself is timed by routing.openstreetmap.de (walking, cycling, driving). Bus and train
aren't covered, so those, and failed lookups, just ask you. Minutes always come from the route.
"""
import math
import os
import re
import time

import requests

from . import config as C

MODES = {"walk": "foot", "cycle": "bike", "drive": "car"}
UA = {"User-Agent": "Sparrow/1.0 (local task app)"}
HERE_FRESH_S = 6 * 3600
last = {"error": ""}
FILLER = re.compile(r"\b(on|in|at|near|by|the|my|local|nearest|closest|one)\b", re.I)
COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _places():
    return C.read_secret("places.json")


def home():
    return _places().get("home", "")


def set_home(address):
    d = _places()
    d["home"] = (address or "").strip()[:120]
    C.write_secret("places.json", d)


def set_here(latlon):
    m = COORDS.match(latlon or "")
    if not m:
        return
    d = _places()
    d["here"] = {"at": f"{float(m.group(1)):.5f},{float(m.group(2)):.5f}", "ts": time.time()}
    C.write_secret("places.json", d)


def here():
    h = _places().get("here") or {}
    return h.get("at", "") if time.time() - h.get("ts", 0) < HERE_FRESH_S else ""


def origin():
    """Where an errand sets off from: where you are now, else the place you gave."""
    return here() or home()


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _search(q, near=None, bounded=False):
    """Matches as (lat, lon, name)."""
    params = {"q": q, "format": "json", "limit": 10 if near else 1}
    if near:  # the "Currys" near you, not one at the other end of the country
        lat, lon = near[0], near[1]
        params["viewbox"] = f"{lon - 0.15},{lat + 0.1},{lon + 0.15},{lat - 0.1}"
        if bounded:
            params["bounded"] = 1
    r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=UA, timeout=15)
    r.raise_for_status()
    return [(float(h["lat"]), float(h["lon"]), h.get("display_name", "")) for h in r.json()]


def _geocode(q, near=None):
    """(lat, lon, name). Coordinates pass straight through. Otherwise try the words as typed, then without
    filler or apostrophes, then just the first word, taking the match closest to you."""
    m = COORDS.match(q or "")
    if m:
        return float(m.group(1)), float(m.group(2)), "where you are"
    clean = re.sub(r"\s+", " ", FILLER.sub(" ", q.replace("'", "").replace("’", ""))).strip()
    tries = [q, clean] + ([clean.split(" ")[0]] if near and clean else [])
    for t in dict.fromkeys(x for x in tries if x):
        for bounded in ((True, False) if near else (False,)):
            hits = _search(t, near, bounded)
            if hits:
                return min(hits, key=lambda h: _km(near, h)) if near else hits[0]
    return None


def _web_address(dest, near):
    """With a Claude key: turn a loose name into a real address using web search. None otherwise."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        from .llm import ask_chat
        where = f"near latitude {near[0]:.3f}, longitude {near[1]:.3f}" if near else "in the UK"
        out = ask_chat("Find the real place the user means. The name may be misspelled or misheard from speech. "
                       "Reply with only its name and full street address with postcode on one line, or NONE.",
                       [{"role": "user", "content": f"\"{dest}\", {where}"}], max_tokens=200, search=True)
    except Exception:
        return None
    lines = (out or "").strip().splitlines()
    line = lines[-1].strip() if lines else ""
    return None if not line or "NONE" in line.upper() else line[:160]


def _osm(start, dest, mode):
    a = _geocode(start)
    if not a:
        raise LookupError(f"couldn't find your starting point \"{start}\" on the map")
    b = None
    addr = _web_address(dest, a)
    if addr:
        b = _geocode(addr, near=a)
    b = b or _geocode(dest, near=a)
    if not b:
        raise LookupError(f"couldn't find \"{dest}\" near you on the map")
    # this server names every profile "driving" in the path; the host picks foot, bike or car
    r = requests.get(f"https://routing.openstreetmap.de/routed-{MODES[mode]}/route/v1/driving/"
                     f"{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "false"}, headers=UA, timeout=15)
    r.raise_for_status()
    routes = r.json().get("routes") or []
    if not routes:
        raise LookupError("found both places but no route between them")
    return int(routes[0]["duration"]), int(routes[0]["distance"]), b[2]


def travel(dest, mode, origin_=None):
    """One-way travel: {"minutes", "km", "to", "via"} or None (reason in last["error"]).
    The minutes come from the route, rounded up, never guessed."""
    start = origin_ or origin()
    last["error"] = ""
    if mode not in MODES:
        last["error"] = "bus and train times aren't on OpenStreetMap"
        return None
    if not start:
        last["error"] = "I don't know where you are. Allow location in the browser, or type where you're setting off from"
        return None
    if not dest:
        return None
    k = f"{start}|{dest}|{mode}".lower()
    cache = _places().get("routes", {})
    if k in cache:
        return cache[k]
    try:
        found = _osm(start, dest, mode)
    except LookupError as e:
        last["error"] = str(e)
        return None
    except requests.RequestException as e:
        last["error"] = f"couldn't reach OpenStreetMap ({type(e).__name__}: {str(e)[:120]})"
        return None
    if not found:
        return None
    secs, metres = found[0], found[1]
    name = found[2] if len(found) > 2 else ""
    out = {"minutes": max(1, -(-secs // 60)), "km": round((metres or 0) / 1000, 1),
           "to": ", ".join(name.split(", ")[:2]), "via": "OpenStreetMap"}
    d = _places()
    d.setdefault("routes", {})[k] = out
    C.write_secret("places.json", d)
    return out
