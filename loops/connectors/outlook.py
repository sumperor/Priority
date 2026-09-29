"""Outlook / Microsoft 365 via Microsoft Graph. Connected from the app (or `run.py auth outlook`).
Work or university accounts may need an IT admin to approve the app."""
import os
from datetime import datetime

import requests

from ..config import MS_TOKEN_CACHE, ms_client_id
from ..models import Message

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read", "User.Read", "Calendars.Read"]
AUTHORITY = "https://login.microsoftonline.com/common"
MAX_PER_FOLDER = 250


def ms_app():
    """Microsoft sign-in client plus its saved token cache (shared by Outlook and Teams)."""
    import msal
    client_id = ms_client_id()
    if not client_id:
        raise RuntimeError("not set up yet: connect it from the app")
    cache = msal.SerializableTokenCache()
    if os.path.exists(MS_TOKEN_CACHE):
        cache.deserialize(open(MS_TOKEN_CACHE).read())
    return msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache), cache


def save_cache(cache):
    if cache.has_state_changed:
        os.makedirs(os.path.dirname(MS_TOKEN_CACHE) or ".", exist_ok=True)
        with open(MS_TOKEN_CACHE, "w") as f:
            f.write(cache.serialize())


def ms_token(scopes, interactive=False):
    app, cache = ms_app()
    accounts = app.get_accounts()
    result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    if not result:
        if not interactive:
            raise RuntimeError("not authorised: connect it again from the app")
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(flow.get("error_description", "device flow failed"))
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    save_cache(cache)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "sign-in failed"))
    return result["access_token"]


class OutlookConnector:
    name = "outlook"

    scopes = SCOPES

    def _token(self, interactive=False) -> str:
        return ms_token(self.scopes, interactive)

    def authenticate(self):
        self._token(interactive=True)
        print("Outlook connected.")

    def fetch(self, since: datetime) -> list[Message]:
        h = {"Authorization": f"Bearer {self._token()}", "Prefer": 'outlook.body-content-type="text"'}
        me = requests.get(f"{GRAPH}/me", headers=h, timeout=30).json()
        my_addr = (me.get("mail") or me.get("userPrincipalName") or "").lower()
        out = []
        for folder in ("inbox", "sentitems"):
            url = f"{GRAPH}/me/mailFolders/{folder}/messages"
            params = {"$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                      "$orderby": "receivedDateTime desc", "$top": "50",
                      "$select": "id,conversationId,subject,from,body,bodyPreview,receivedDateTime"}
            n = 0
            while url and n < MAX_PER_FOLDER:
                r = requests.get(url, headers=h, params=params, timeout=30)
                r.raise_for_status()
                d = r.json()
                for m in d.get("value", []):
                    frm = (m.get("from") or {}).get("emailAddress", {})
                    addr = (frm.get("address") or "").lower()
                    out.append(Message(
                        source="outlook", msg_id=m["id"], thread_id=m["conversationId"], sender=addr,
                        sender_name=frm.get("name") or addr,
                        is_from_me=(folder == "sentitems" or addr == my_addr),
                        text=((m.get("body") or {}).get("content") or m.get("bodyPreview", ""))[:4000],
                        ts=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
                        subject=m.get("subject") or ""))
                    n += 1
                url, params = d.get("@odata.nextLink"), None
        return out

    def fetch_jobs(self, since, known=frozenset()):
        from ..leads import is_job_alert, is_recruiter
        return [m for m in self.fetch(since) if m.msg_id not in known and (is_job_alert(m.sender, m.sender_name, m.subject) or is_recruiter(m))]

    def events(self, start: datetime, end: datetime) -> list[dict]:
        h = {"Authorization": f"Bearer {self._token()}", "Prefer": 'outlook.timezone="UTC"'}
        r = requests.get(f"{GRAPH}/me/calendarview", headers=h, timeout=30, params={
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": "subject,start,end,isCancelled", "$orderby": "start/dateTime", "$top": "20"})
        r.raise_for_status()
        return [{"title": e.get("subject") or "Busy", "start": e["start"]["dateTime"][:19] + "+00:00",
                 "end": e["end"]["dateTime"][:19] + "+00:00", "source": "outlook"}
                for e in r.json().get("value", []) if not e.get("isCancelled")]
