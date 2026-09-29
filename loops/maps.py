"""Travel time from A to B, looked up instead of asked.

Free OpenStreetMap, no key: Nominatim finds the places, routing.openstreetmap.de times the route
for walking, cycling and driving. Bus and train aren't covered, so those (and failed lookups) just ask you.
Your starting point ("home") is saved once in secrets/places.json.
"""
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


def _geocode(q, near=None):
    params = {"q": q, "format": "json", "limit": 1}
    if near:  # look for "Tesco" close to home, not anywhere in the country
        lat, lon = near
        params.update(viewbox=f"{lon - 0.1},{lat + 0.07},{lon + 0.1},{lat - 0.07}", bounded=1)
    r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=UA, timeout=15)
    r.raise_for_status()
    hits = r.json()
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def _osm(origin, dest, mode):
    profile = MODES[mode]
    a = _geocode(origin)
    b = a and (_geocode(dest, near=a) or _geocode(dest))
    if not b:
        return None
    r = requests.get(f"https://routing.openstreetmap.de/routed-{profile}/route/v1/{profile}/"
                     f"{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "false"}, headers=UA, timeout=15)
    r.raise_for_status()
    routes = r.json().get("routes") or []
    return (int(routes[0]["duration"]), int(routes[0]["distance"])) if routes else None


def travel(dest, mode, origin=None):
    """One-way travel: {"minutes", "km", "via"} or None. The minutes are rounded up, never guessed."""
    origin = origin or home()
    if not (origin and dest and mode in MODES):
        return None
    cache = C.read_secret("places.json").setdefault("routes", {})
    k = f"{origin}|{dest}|{mode}".lower()
    if k in cache:
        return cache[k]
    try:
        found = _osm(origin, dest, mode)
    except Exception as e:
        last["error"] = str(e)[:150]
        found = None
    if not found:
        return None
    secs, metres = found
    out = {"minutes": max(1, -(-secs // 60)), "km": round((metres or 0) / 1000, 1), "via": "OpenStreetMap"}
    d = C.read_secret("places.json")
    d.setdefault("routes", {})[k] = out
    C.write_secret("places.json", d)
    return out
