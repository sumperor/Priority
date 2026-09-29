"""Travel time from A to B, looked up instead of asked.

Free OpenStreetMap, no key: Nominatim finds the places, routing.openstreetmap.de times the route
for walking, cycling and driving. Bus and train aren't covered, so those (and failed lookups) just ask you.
Your starting point ("home") is saved once in secrets/places.json.
"""
import re

import requests

from . import config as C

MODES = {"walk": "foot", "cycle": "bike", "drive": "car"}
UA = {"User-Agent": "Sparrow/1.0 (local task app)"}
last = {"error": ""}


def home():
    return C.read_secret("places.json").get("home", "")


def set_home(address):
    d = C.read_secret("places.json")
    d["home"] = (address or "").strip()[:120]
    C.write_secret("places.json", d)


FILLER = re.compile(r"\b(on|in|at|near|by|the|my|local|nearest|closest|one)\b", re.I)


def _search(q, near=None, bounded=False):
    params = {"q": q, "format": "json", "limit": 1}
    if near:  # prefer the "Tesco" near you, not one at the other end of the country
        lat, lon = near
        params["viewbox"] = f"{lon - 0.1},{lat + 0.07},{lon + 0.1},{lat - 0.07}"
        if bounded:
            params["bounded"] = 1
    r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=UA, timeout=15)
    r.raise_for_status()
    hits = r.json()
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def _geocode(q, near=None):
    """Try the words as typed, then without filler ("Tesco on the high street" -> "Tesco high street"),
    then just the first word ("Tesco"), each close to home first."""
    tries = [q, re.sub(r"\s+", " ", FILLER.sub(" ", q)).strip()]
    if near:
        tries.append(tries[-1].split(" ")[0])
    for t in dict.fromkeys(x for x in tries if x):
        for bounded in ((True, False) if near else (False,)):
            hit = _search(t, near, bounded)
            if hit:
                return hit
    return None


def _osm(origin, dest, mode):
    a = _geocode(origin)
    if not a:
        raise LookupError(f"couldn't find your starting point \"{origin}\" on the map")
    b = _geocode(dest, near=a)
    if not b:
        raise LookupError(f"couldn't find \"{dest}\" near you on the map")
    # this server names every profile "driving" in the path; the host picks foot, bike or car
    r = requests.get(f"https://routing.openstreetmap.de/routed-{MODES[mode]}/route/v1/driving/"
                     f"{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "false"}, headers=UA, timeout=15)
    r.raise_for_status()
    routes = r.json().get("routes") or []
    if not routes:
        raise LookupError("found both places but no route between them")
    return int(routes[0]["duration"]), int(routes[0]["distance"])


def travel(dest, mode, origin=None):
    """One-way travel: {"minutes", "km", "via"} or None. The minutes are rounded up, never guessed."""
    origin = origin or home()
    last["error"] = ""
    if mode not in MODES:
        last["error"] = "bus and train times aren't on OpenStreetMap"
        return None
    if not origin:
        last["error"] = "I don't know where you set off from yet"
        return None
    if not dest:
        return None
    cache = C.read_secret("places.json").setdefault("routes", {})
    k = f"{origin}|{dest}|{mode}".lower()
    if k in cache:
        return cache[k]
    try:
        found = _osm(origin, dest, mode)
    except LookupError as e:
        last["error"] = str(e)
        found = None
    except requests.RequestException as e:
        last["error"] = f"couldn't reach OpenStreetMap ({type(e).__name__}: {str(e)[:120]})"
        found = None
    if not found:
        return None
    secs, metres = found
    out = {"minutes": max(1, -(-secs // 60)), "km": round((metres or 0) / 1000, 1), "via": "OpenStreetMap"}
    d = C.read_secret("places.json")
    d.setdefault("routes", {})[k] = out
    C.write_secret("places.json", d)
    return out
