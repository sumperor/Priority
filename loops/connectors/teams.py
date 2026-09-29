"""Microsoft Teams chats via Microsoft Graph. Uses the same Microsoft sign-in as Outlook,
plus the Chat.Read permission. Works for work and university accounts; personal Teams isn't supported by Graph."""
import html
import re
from datetime import datetime

import requests

from ..models import Message
from .outlook import GRAPH, ms_token

SCOPES = ["Chat.Read", "User.Read"]
MAX_CHATS = 40


def _text(body):
    t = (body or {}).get("content") or ""
    if (body or {}).get("contentType") == "html":
        t = re.sub(r"(?i)<br\s*/?>|</p>", "\n", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", html.unescape(t)).strip()


class TeamsConnector:
    name = "teams"
    scopes = SCOPES

    def _token(self, interactive=False):
        return ms_token(SCOPES, interactive)

    def authenticate(self):
        self._token(interactive=True)
        print("Teams connected.")

    def fetch(self, since: datetime) -> list[Message]:
        h = {"Authorization": f"Bearer {self._token()}"}
        me = requests.get(f"{GRAPH}/me", headers=h, timeout=30).json().get("id", "")
        r = requests.get(f"{GRAPH}/me/chats", headers=h, timeout=30,
                         params={"$top": "50", "$orderby": "lastMessagePreview/createdDateTime desc"})
        r.raise_for_status()
        out = []
        for chat in r.json().get("value", [])[:MAX_CHATS]:
            updated = chat.get("lastUpdatedDateTime")
            if updated and datetime.fromisoformat(updated.replace("Z", "+00:00")) < since:
                continue
            mr = requests.get(f"{GRAPH}/me/chats/{chat['id']}/messages", headers=h, timeout=30, params={"$top": "30"})
            if not mr.ok:
                continue
            for m in mr.json().get("value", []):
                if m.get("messageType") != "message" or not (m.get("from") or {}).get("user"):
                    continue
                ts = datetime.fromisoformat(m["createdDateTime"].replace("Z", "+00:00"))
                if ts < since:
                    continue
                u = m["from"]["user"]
                out.append(Message(
                    source="teams", msg_id=m["id"], thread_id=chat["id"], sender=u.get("id", ""),
                    sender_name=u.get("displayName") or "Someone", is_from_me=(u.get("id") == me),
                    text=_text(m.get("body"))[:4000], ts=ts, subject=chat.get("topic") or ""))
        return out
