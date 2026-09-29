"""Connect accounts from the app: click Connect, sign in, come back connected.

Google and Microsoft need the app registered once by its owner (a client file or an app ID).
After that, connecting is a normal sign-in in the browser. Slack only allows sign-in to https
addresses, so for a local app you paste your Slack token once. iMessage is read from this Mac.
"""
import json
import os
import secrets as _secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import config as C

router = APIRouter()
_pending = {}              # sign-ins in progress, by OAuth state
after_connect = None       # set by the server: runs a sync straight after connecting
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]
OUTLOOK_SCOPES = ["Mail.Read", "User.Read", "Calendars.Read"]
TEAMS_SCOPES = ["Chat.Read", "User.Read"]


def _base():
    return f"http://localhost:{C.PORT}"


def _home():
    return f"http://127.0.0.1:{C.PORT}"  # the app's own address, so saved settings in the browser carry over


def _marks():
    return set(C.read_secret("connected.json").get("on", []))


def _mark(name, on=True):
    m = _marks()
    (m.add if on else m.discard)(name)
    C.write_secret("connected.json", {"on": sorted(m)})


def registered(name):
    """Has the app been registered with this provider (a one-time job for the app's owner)?"""
    if name == "gmail":
        return os.path.exists(C.GMAIL_CREDENTIALS)
    if name in ("outlook", "teams"):
        return bool(C.ms_client_id())
    return True


def is_connected(name):
    if name in _marks():
        return True
    # Accounts connected the old way, from Terminal
    if name == "gmail":
        return os.path.exists(C.GMAIL_TOKEN)
    if name == "outlook":
        return os.path.exists(C.MS_TOKEN_CACHE) and "teams" not in _marks()
    if name == "slack":
        return bool(os.getenv("SLACK_USER_TOKEN"))
    return False


def offered():
    """Account types shown in the app on this machine."""
    from .connectors import imessage
    return [n for n in C.CONNECTORS if n != "imessage" or imessage.available()]


def connected():
    return [n for n in offered() if is_connected(n)]


def _done(name):
    _mark(name)
    if after_connect:
        after_connect()
    return RedirectResponse(f"{_home()}/?connected={name}", status_code=303)


def _failed(name, why):
    from urllib.parse import quote
    return RedirectResponse(f"{_home()}/?connect_failed={name}&why={quote(why[:200])}", status_code=303)


# ---------------------------------------------------------------- status
@router.get("/api/connect")
def status():
    from .connectors import LABELS
    return [{"name": n, "label": LABELS.get(n, n), "registered": registered(n), "connected": is_connected(n),
             "redirect": f"{_base()}/oauth/{'google' if n == 'gmail' else 'microsoft'}"
             if n in ("gmail", "outlook", "teams") else None} for n in offered()]


# ---------------------------------------------------------------- one-time app registration
class GoogleApp(BaseModel):
    json_text: str


@router.post("/api/connect/gmail/app")
def gmail_app(body: GoogleApp):
    try:
        d = json.loads(body.json_text)
        info = d.get("installed") or d.get("web")
        assert info and info.get("client_id") and info.get("client_secret")
    except Exception:
        raise HTTPException(400, "That isn't the Google client file. Download it again from Google Cloud, "
                                 "under Clients, with type Desktop app.")
    os.makedirs(os.path.dirname(C.GMAIL_CREDENTIALS) or ".", exist_ok=True)
    with open(C.GMAIL_CREDENTIALS, "w") as f:
        json.dump(d, f)
    return {"ok": True}


class MsApp(BaseModel):
    client_id: str


@router.post("/api/connect/microsoft/app")
def microsoft_app(body: MsApp):
    cid = body.client_id.strip()
    import re
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", cid):
        raise HTTPException(400, "That doesn't look like an Application (client) ID. It's 36 characters, like 1a2b3c4d-....")
    C.write_secret("microsoft_app.json", {"client_id": cid})
    return {"ok": True}


# ---------------------------------------------------------------- Google (Gmail + Calendar)
@router.get("/api/connect/gmail/start")
def gmail_start():
    if not registered("gmail"):
        raise HTTPException(400, "Set up Gmail first.")
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")   # the redirect is to this computer only
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(C.GMAIL_CREDENTIALS, scopes=GMAIL_SCOPES, redirect_uri=f"{_base()}/oauth/google")
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _pending[state] = {"kind": "google", "flow": flow}
    return RedirectResponse(url, status_code=303)


@router.get("/oauth/google")
def google_back(request: Request):
    p = _pending.pop(request.query_params.get("state", ""), None)
    if request.query_params.get("error"):
        return _failed("gmail", "You cancelled the Google sign-in.")
    if not p:
        return _failed("gmail", "That sign-in expired. Try again.")
    try:
        p["flow"].fetch_token(code=request.query_params["code"])
        os.makedirs(os.path.dirname(C.GMAIL_TOKEN) or ".", exist_ok=True)
        with open(C.GMAIL_TOKEN, "w") as f:
            f.write(p["flow"].credentials.to_json())
    except Exception as e:
        return _failed("gmail", f"Google sign-in failed: {e}")
    return _done("gmail")


# ---------------------------------------------------------------- Microsoft (Outlook, Teams)
@router.get("/api/connect/{which}/start")
def microsoft_start(which: str):
    if which not in ("outlook", "teams"):
        raise HTTPException(404, "Unknown account type")
    if not registered(which):
        raise HTTPException(400, "Set up Microsoft first.")
    from .connectors.outlook import ms_app
    app, cache = ms_app()
    flow = app.initiate_auth_code_flow(OUTLOOK_SCOPES if which == "outlook" else TEAMS_SCOPES,
                                       redirect_uri=f"{_base()}/oauth/microsoft", state=_secrets.token_urlsafe(16))
    _pending[flow["state"]] = {"kind": "microsoft", "which": which, "flow": flow}
    return RedirectResponse(flow["auth_uri"], status_code=303)


@router.get("/oauth/microsoft")
def microsoft_back(request: Request):
    q = dict(request.query_params)
    p = _pending.pop(q.get("state", ""), None)
    which = (p or {}).get("which", "outlook")
    if q.get("error"):
        return _failed(which, q.get("error_description") or "You cancelled the Microsoft sign-in.")
    if not p:
        return _failed(which, "That sign-in expired. Try again.")
    from .connectors.outlook import ms_app, save_cache
    app, cache = ms_app()
    r = app.acquire_token_by_auth_code_flow(p["flow"], q)
    if "access_token" not in r:
        return _failed(which, r.get("error_description") or "Microsoft sign-in failed.")
    save_cache(cache)
    return _done(which)


# ---------------------------------------------------------------- Slack (token pasted once)
class Token(BaseModel):
    token: str


@router.post("/api/connect/slack")
def slack_connect(body: Token):
    t = body.token.strip()
    if not t.startswith("xoxp-"):
        raise HTTPException(400, "Use the User OAuth Token. It starts with xoxp-.")
    try:
        from slack_sdk import WebClient
        who = WebClient(token=t).auth_test()
    except Exception as e:
        raise HTTPException(400, f"Slack didn't accept that token: {e}")
    C.write_secret("slack.json", {"token": t})
    _mark("slack")
    if after_connect:
        after_connect()
    return {"ok": True, "user": who.get("user"), "team": who.get("team")}


# ---------------------------------------------------------------- iMessage (this Mac)
@router.post("/api/connect/imessage")
def imessage_connect():
    from .connectors.imessage import can_read
    ok, why = can_read()
    if not ok:
        raise HTTPException(400, why)
    _mark("imessage")
    if after_connect:
        after_connect()
    return {"ok": True}


# ---------------------------------------------------------------- disconnect
@router.post("/api/connect/{name}/disconnect")
def disconnect(name: str):
    if name not in offered():
        raise HTTPException(404, "Unknown account type")
    _mark(name, False)
    if name == "gmail" and os.path.exists(C.GMAIL_TOKEN):
        os.remove(C.GMAIL_TOKEN)
    if name == "slack" and os.path.exists(C.secret_path("slack.json")):
        os.remove(C.secret_path("slack.json"))
    if name in ("outlook", "teams") and not ({"outlook", "teams"} & _marks()) and os.path.exists(C.MS_TOKEN_CACHE):
        os.remove(C.MS_TOKEN_CACHE)   # signed out of Microsoft once neither is connected
    from .engine import SOURCES
    SOURCES.pop(name, None)
    return {"ok": True}
