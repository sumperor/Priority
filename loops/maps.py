"""Travel time from A to B, looked up instead of asked.

Google Maps (Routes API) when GOOGLE_MAPS_API_KEY is set, which also covers bus and train.
Otherwise free OpenStreetMap: Nominatim finds the places, routing.openstreetmap.de times the route
for walking, cycling and driving. If neither works the errand just asks you, as before.
Your starting point ("home") is saved once in secrets/places.json.
"""
import os

import requests

from . import config as C

MODES = {"walk": ("WALK", "foot"), "cycle": ("BICYCLE", "bike"), "drive": ("DRIVE", "car"), "transit": ("TRANSIT", None)}
UA = {"User-Agent": "Sparrow/1.0 (local task app)"}
last = {"error": ""}


def home():
    return C.read_secret("places.json").get("home", "")


def set_home(address):
    d = C.read_secret("places.json")
    d["home"] = (address or "").strip()[:120]
    C.write_secret("places.json", d)


def _google(origin, dest, mode, key):
    r = requests.post("https://routes.googleapis.com/directions/v2:computeRoutes", timeout=15,
                      headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "routes.duration,routes.distanceMeters"},
                      json={"origin": {"address": origin}, "destination": {"address": dest}, "travelMode": MODES[mode][0]})
    r.raise_for_status()
    routes = r.json().get("routes") or []
    if not routes:
        return None
    return int(routes[0]["duration"].rstrip("s")), routes[0].get("distanceMeters")


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
    profile = MODES[mode][1]
    if not profile:
        return None
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
    key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    found, via = None, ""
    for name, fn in (("Google Maps", lambda: _google(origin, dest, mode, key) if key else None),
                     ("OpenStreetMap", lambda: _osm(origin, dest, mode))):
        try:
            found = fn()
        except Exception as e:
            last["error"] = f"{name}: {str(e)[:150]}"
            found = None
        if found:
            via = name
            break
    if not found:
        return None
    secs, metres = found
    out = {"minutes": max(1, -(-secs // 60)), "km": round((metres or 0) / 1000, 1), "via": via}
    d = C.read_secret("places.json")
    d.setdefault("routes", {})[k] = out
    C.write_secret("places.json", d)
    return out
