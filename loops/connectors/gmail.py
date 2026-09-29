"""Gmail: read-only. Personal use runs as an unverified 'testing' app (you as a test user)."""
import base64
import os
from datetime import datetime, timezone
from email.utils import parseaddr

from ..config import GMAIL_CREDENTIALS, GMAIL_TOKEN
from ..leads import is_job_alert
from ..models import Message

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]
MAX_MESSAGES = 500


def _body(payload) -> str:
    """Depth-first search for the first text/plain part."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        t = _body(part)
        if t:
            return t
    return ""


class GmailConnector:
    name = "gmail"

    def _creds(self, interactive=False):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN, SCOPES) if os.path.exists(GMAIL_TOKEN) else None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError("not authorised: connect it again from the app")
            creds = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, SCOPES).run_local_server(port=0)
        os.makedirs(os.path.dirname(GMAIL_TOKEN) or ".", exist_ok=True)
        with open(GMAIL_TOKEN, "w") as f:
            f.write(creds.to_json())
        return creds

    def authenticate(self):
        self._creds(interactive=True)
        print("Gmail connected.")

    def fetch(self, since: datetime) -> list[Message]:
        from googleapiclient.discovery import build

        svc = build("gmail", "v1", credentials=self._creds(), cache_discovery=False)
        me = svc.users().getProfile(userId="me").execute()["emailAddress"].lower()
        q = f"after:{int(since.timestamp())} -category:promotions -category:social -category:forums"
        ids, page = [], None
        while len(ids) < MAX_MESSAGES:
            r = svc.users().messages().list(userId="me", q=q, pageToken=page, maxResults=100).execute()
            ids += [m["id"] for m in r.get("messages", [])]
            page = r.get("nextPageToken")
            if not page:
                break

        out = []
        for mid in ids[:MAX_MESSAGES]:
            m = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            h = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
            name, addr = parseaddr(h.get("from", ""))
            addr = addr.lower()
            if "list-unsubscribe" in h and addr != me and not is_job_alert(addr, name, h.get("subject", "")):
                continue  # newsletters and bulk mail never create loops; job alerts become leads
            out.append(Message(
                source="gmail", msg_id=mid, thread_id=m["threadId"], sender=addr,
                sender_name=name or addr, is_from_me=(addr == me),
                text=(_body(m["payload"]) or m.get("snippet", ""))[:4000],
                ts=datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc),
                subject=h.get("subject", "")))
        return out

    def events(self, start: datetime, end: datetime) -> list[dict]:
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=self._creds(), cache_discovery=False)
        r = svc.events().list(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                              singleEvents=True, orderBy="startTime", maxResults=20).execute()
        out = []
        for e in r.get("items", []):
            s, en = e.get("start", {}), e.get("end", {})
            if "dateTime" not in s:
                continue  # all-day events don't block time
            out.append({"title": e.get("summary") or "Busy", "start": s["dateTime"], "end": en.get("dateTime"),
                        "source": "google"})
        return out
