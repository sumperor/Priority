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
last = {"error": "", "kind": ""}   # kind: start | dest | net | mode
_geo_memo, _clock = {}, {"t": 0.0}
FILLER = re.compile(r"\b(on|in|at|near|by|the|my|local|nearest|closest|one)\b", re.I)
# what people say before the place: "it is Currys in Farnborough", "I'm at the station", "going to Tesco"
CHATTER = re.compile(r"^\s*((it'?s|it is|i'?m|i am|we'?re|we are|currently|right now|now|just|at|from|near|in|"
                     r"going to|go to|heading to|setting off from|starting from|leaving from)\s+)+", re.I)
COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _places():
    return C.read_secret("places.json")


def home():
    return _places().get("home", "")


def set_home(address):
    d = _places()
    d["home"] = (address or "").strip()[:120]
    C.write_secret("places.json", d)


def set_here(at):
    """Where you are right now: browser coordinates "lat,lon", or what you typed. Good for a few hours."""
    at = (at or "").strip()
    m = COORDS.match(at)
    if m:
        at = f"{float(m.group(1)):.5f},{float(m.group(2)):.5f}"
    if not at:
        return
    d = _places()
    d["here"] = {"at": at[:120], "ts": time.time()}
    C.write_secret("places.json", d)


def here():
    h = _places().get("here") or {}
    return h.get("at", "") if time.time() - h.get("ts", 0) < HERE_FRESH_S else ""


def mac_location():
    """Where this Mac is, from macOS Location Services, via the free CoreLocationCLI tool
    (brew install corelocationcli). "lat,lon" or "" if it isn't installed or isn't allowed."""
    import shutil
    import subprocess
    exe = shutil.which("CoreLocationCLI")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"(-?\d{1,2}\.\d+)[,\s]+(-?\d{1,3}\.\d+)", out or "")
    return f"{m.group(1)},{m.group(2)}" if m else ""


def origin():
    """Where an errand sets off from: where you said you are, in the last few hours."""
    return here()


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
    key = (q.lower(), tuple(round(x, 2) for x in near[:2]) if near else None, bounded)
    if key in _geo_memo:
        return _geo_memo[key]
    wait = 1.05 - (time.time() - _clock["t"])   # OpenStreetMap asks for at most one search a second
    if wait > 0:
        time.sleep(wait)
    _clock["t"] = time.time()
    r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=UA, timeout=15)
    r.raise_for_status()
    _geo_memo[key] = out = [(float(h["lat"]), float(h["lon"]), h.get("display_name", "")) for h in r.json()]
    return out


def variants(q):
    """Ways to write a place that OpenStreetMap's search is likely to understand, best first.
    "it is Currys in Farnborough" -> "Currys, Farnborough"; "Aldershot Tesco" -> "Tesco, Aldershot"."""
    q = CHATTER.sub("", (q or "").strip()).strip(" .,")
    town = re.sub(r"\s+in\s+", ", ", q)
    clean = re.sub(r"\s+", " ", FILLER.sub(" ", town.replace("'", "").replace("\u2019", ""))).strip(" ,")
    clean = re.sub(r"\s*,\s*", ", ", clean)
    out = [town, clean]
    words = clean.replace(",", "").split()
    if "," not in clean and len(words) == 2:
        out += [f"{words[1]}, {words[0]}", f"{words[0]}, {words[1]}"]
    elif "," not in clean and len(words) > 2:
        out += [f"{' '.join(words[1:])}, {words[0]}", f"{' '.join(words[:-1])}, {words[-1]}"]
    return [x for x in dict.fromkeys(out) if x]


def _geocode(q, near=None):
    """(lat, lon, name). Coordinates pass straight through. Otherwise try the ways of writing it,
    then just the first word ("Currys"), taking the match closest to you."""
    m = COORDS.match(q or "")
    if m:
        return float(m.group(1)), float(m.group(2)), "where you are"
    tries = variants(q)
    if near and tries:
        tries.append(tries[-1].split(",")[0].split(" ")[0])
    for t in dict.fromkeys(tries):
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
        last["kind"] = "start"
        raise LookupError(f"couldn't find \"{start}\" on the map")
    b = None
    addr = _web_address(dest, a)
    if addr:
        b = _geocode(addr, near=a)
    b = b or _geocode(dest, near=a)
    if not b:
        last["kind"] = "dest"
        raise LookupError(f"couldn't find \"{dest}\" on the map")
    # this server names every profile "driving" in the path; the host picks foot, bike or car
    r = requests.get(f"https://routing.openstreetmap.de/routed-{MODES[mode]}/route/v1/driving/"
                     f"{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "false"}, headers=UA, timeout=15)
    r.raise_for_status()
    routes = r.json().get("routes") or []
    if not routes:
        raise LookupError("found both places but no route between them")
    return int(routes[0]["duration"]), int(routes[0]["distance"]), b[2], b[0], b[1]


def travel(dest, mode, origin_=None):
    """One-way travel: {"minutes", "km", "to", "via"} or None (reason in last["error"]).
    The minutes come from the route, rounded up, never guessed."""
    start = origin_ or origin()
    last.update(error="", kind="")
    if mode not in MODES:
        last.update(error="bus and train times aren't on OpenStreetMap", kind="mode")
        return None
    if not start:
        last["kind"] = "start"
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
        last["kind"] = "net"
        last["error"] = f"couldn't reach OpenStreetMap ({type(e).__name__}: {str(e)[:120]})"
        return None
    if not found:
        return None
    secs, metres = found[0], found[1]
    name = found[2] if len(found) > 2 else ""
    out = {"minutes": max(1, -(-secs // 60)), "km": round((metres or 0) / 1000, 1),
           "to": ", ".join(name.split(", ")[:3]), "via": "OpenStreetMap"}
    if len(found) > 4:
        out.update(lat=found[3], lon=found[4])
    d = _places()
    d.setdefault("routes", {})[k] = out
    C.write_secret("places.json", d)
    return out
