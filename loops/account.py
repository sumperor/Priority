"""Accounts for the Sparrow website: sign up, log in, log out.

Stored on this computer (secrets/accounts.json). Passwords are salted and hashed (PBKDF2-SHA256),
never stored as typed. The app itself doesn't require an account while it runs locally.
Paid plans are recorded as the plan you chose; there's no checkout yet.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

from . import config as C

FILE = "accounts.json"
ROUNDS = 200_000
PLANS = ("free", "pro")


def _load():
    d = C.read_secret(FILE)
    d.setdefault("users", {})
    d.setdefault("sessions", {})
    return d


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ROUNDS).hex()


def _public(email, u):
    return {"email": email, "name": u["name"], "plan": u.get("plan", "free"), "wanted": u.get("wanted", "free")}


def signup(name, email, password, plan="free"):
    email = (email or "").strip().lower()
    name = (name or "").strip()[:60]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("That email address doesn't look right.")
    if len(password or "") < 8:
        raise ValueError("Use at least 8 characters for your password.")
    d = _load()
    if email in d["users"]:
        raise ValueError("There's already an account with that email. Log in instead.")
    salt = secrets.token_hex(16)
    d["users"][email] = {"name": name or email.split("@")[0], "salt": salt, "hash": _hash(password, salt),
                         "plan": "free", "wanted": plan if plan in PLANS else "free",
                         "created": datetime.now(timezone.utc).isoformat()}
    token = secrets.token_urlsafe(32)
    d["sessions"][token] = email
    C.write_secret(FILE, d)
    return token, _public(email, d["users"][email])


def login(email, password):
    email = (email or "").strip().lower()
    d = _load()
    u = d["users"].get(email)
    if not u or not hmac.compare_digest(u["hash"], _hash(password or "", u["salt"])):
        raise ValueError("That email and password don't match.")
    token = secrets.token_urlsafe(32)
    d["sessions"][token] = email
    C.write_secret(FILE, d)
    return token, _public(email, u)


def me(token):
    if not token:
        return None
    d = _load()
    email = d["sessions"].get(token)
    return _public(email, d["users"][email]) if email in d["users"] else None


def logout(token):
    d = _load()
    d["sessions"].pop(token or "", None)
    C.write_secret(FILE, d)
