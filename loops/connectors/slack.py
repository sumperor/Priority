"""Slack DMs and group DMs using a user token (xoxp-).
Slack limits history access for apps not in its Marketplace; this backs off on rate limits."""
import time
from datetime import datetime, timezone

from ..config import SLACK_USER_TOKEN
from ..models import Message


class SlackConnector:
    name = "slack"

    def _client(self):
        from slack_sdk import WebClient
        if not SLACK_USER_TOKEN:
            raise RuntimeError("set SLACK_USER_TOKEN in .env")
        return WebClient(token=SLACK_USER_TOKEN)

    def _call(self, fn, **kw):
        from slack_sdk.errors import SlackApiError
        for _ in range(3):
            try:
                return fn(**kw)
            except SlackApiError as e:
                if e.response.status_code == 429:
                    time.sleep(int(e.response.headers.get("Retry-After", 30)))
                    continue
                raise
        raise RuntimeError("Slack rate limit: try again later")

    def authenticate(self):
        r = self._client().auth_test()
        print(f"Slack connected as {r['user']} in {r['team']}.")

    def fetch(self, since: datetime) -> list[Message]:
        c = self._client()
        me = self._call(c.auth_test)["user_id"]
        names = {}

        def name(uid):
            if uid not in names:
                try:
                    u = self._call(c.users_info, user=uid)["user"]
                    names[uid] = u.get("real_name") or u.get("name") or uid
                except Exception:
                    names[uid] = uid
            return names[uid]

        convs, cursor = [], None
        while True:
            r = self._call(c.conversations_list, types="im,mpim", limit=200, cursor=cursor, exclude_archived=True)
            convs += r["channels"]
            cursor = r.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        out = []
        for ch in convs:
            hist = self._call(c.conversations_history, channel=ch["id"], oldest=str(since.timestamp()), limit=100)
            for m in hist.get("messages", []):
                if m.get("subtype") or not m.get("user"):
                    continue  # joins, bots, edits
                uid = m["user"]
                out.append(Message(
                    source="slack", msg_id=f"{ch['id']}:{m['ts']}", thread_id=ch["id"], sender=uid,
                    sender_name=name(uid), is_from_me=(uid == me), text=m.get("text", "")[:4000],
                    ts=datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc)))
        return out
