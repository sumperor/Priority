"""Connecting accounts from the app: Google sign-in link, Slack token, iMessage on a Mac, disconnect."""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def client(tmp_path, monkeypatch):
    from loops import config as C
    sec = tmp_path / "secrets"
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(C, "CONNECTORS", ["gmail", "outlook", "teams", "slack", "imessage"])
    monkeypatch.setattr(C, "SECRETS_DIR", str(sec))
    monkeypatch.setattr(C, "GMAIL_TOKEN", str(sec / "gmail_token.json"))
    monkeypatch.setattr(C, "GMAIL_CREDENTIALS", str(sec / "gmail_credentials.json"))
    monkeypatch.setattr(C, "MS_TOKEN_CACHE", str(sec / "ms.json"))
    for k in ("ANTHROPIC_API_KEY", "SLACK_USER_TOKEN", "MS_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from loops import connect
    from loops.server import app
    monkeypatch.setattr(connect, "after_connect", None)   # don't start a real sync
    return TestClient(app)


def test_google_and_microsoft_setup(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    st = {x["name"]: x for x in c.get("/api/connect").json()}
    assert not st["gmail"]["registered"] and not st["gmail"]["connected"]
    assert c.post("/api/connect/gmail/app", json={"json_text": '{"nope": 1}'}).status_code == 400
    creds = {"installed": {"client_id": "abc.apps.googleusercontent.com", "client_secret": "s",
                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                           "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}
    assert c.post("/api/connect/gmail/app", json={"json_text": json.dumps(creds)}).status_code == 200
    r = c.get("/api/connect/gmail/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Foauth%2Fgoogle" in r.headers["location"]
    # Coming back without a matching sign-in fails politely
    r = c.get("/oauth/google?state=bad&code=x", follow_redirects=False)
    assert "connect_failed=gmail" in r.headers["location"]

    assert c.post("/api/connect/microsoft/app", json={"client_id": "not-an-id"}).status_code == 400
    assert c.post("/api/connect/microsoft/app", json={"client_id": "1a2b3c4d-1111-2222-3333-444455556666"}).status_code == 200
    st = {x["name"]: x for x in c.get("/api/connect").json()}
    assert st["outlook"]["registered"] and st["teams"]["registered"] and not st["outlook"]["connected"]


def test_slack_token_and_disconnect(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    import slack_sdk

    class FakeClient:
        def __init__(self, token): self.token = token
        def auth_test(self): return {"user": "sumedh", "team": "Loops"}
    monkeypatch.setattr(slack_sdk, "WebClient", FakeClient)
    assert c.post("/api/connect/slack", json={"token": "xoxb-bot"}).status_code == 400
    r = c.post("/api/connect/slack", json={"token": "xoxp-123"}).json()
    assert r["team"] == "Loops"
    src = {x["name"]: x for x in c.get("/api/state").json()["sources"]}
    assert src["slack"]["state"] in ("waiting", "checking") and src["gmail"]["state"] == "off"
    from loops.config import slack_token
    assert slack_token() == "xoxp-123"
    c.post("/api/connect/slack/disconnect")
    assert slack_token() == "" and c.get("/api/state").json()["sources"][3]["state"] == "off"


def test_imessage_from_a_mac_database(tmp_path, monkeypatch):
    db = tmp_path / "chat.db"
    x = sqlite3.connect(db)
    x.executescript("""CREATE TABLE message(ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER,
                         is_from_me INTEGER, handle_id INTEGER);
                       CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
                       CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT);
                       CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);""")
    apple = datetime(2001, 1, 1, tzinfo=timezone.utc)
    ns = int((datetime.now(timezone.utc) - timedelta(minutes=5) - apple).total_seconds() * 1e9)
    body = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x1fCan you send me the deck today?"
    x.execute("INSERT INTO handle VALUES(1, '+447700900123')")
    x.execute("INSERT INTO chat VALUES(1, '+447700900123', '')")
    x.execute("INSERT INTO message VALUES(1, NULL, ?, ?, 0, 1)", (body, ns))
    x.execute("INSERT INTO message VALUES(2, 'old one', NULL, ?, 0, 1)", (ns - int(30 * 864e11),))
    x.execute("INSERT INTO chat_message_join VALUES(1, 1)")
    x.commit(); x.close()

    from loops.connectors import imessage
    monkeypatch.setattr(imessage, "DB", str(db))
    monkeypatch.setenv("IMESSAGE_DB", str(db))
    c = client(tmp_path, monkeypatch)
    assert c.post("/api/connect/imessage").status_code == 200
    msgs = imessage.IMessageConnector().fetch(datetime.now(timezone.utc) - timedelta(days=1))
    assert [m.text for m in msgs] == ["Can you send me the deck today?"]
    assert msgs[0].sender == "+447700900123" and not msgs[0].is_from_me


def test_gmail_only_fetches_new_and_errors_are_plain(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone
    from loops import engine
    from loops.connect import _mark
    from loops.decisions import RuleDecisions
    from loops.models import Message
    from loops.server import db
    now = datetime.now(timezone.utc)

    class FakeGmail:
        name = "gmail"
        calls = []
        def fetch(self, since, known=frozenset()):
            self.calls.append(set(known))
            self.skipped = ["news1"]
            return [Message("gmail", i, i, "a@x.com", "A", False, "hi", now, "s") for i in ("m1", "m2") if i not in known]

    g = FakeGmail()
    monkeypatch.setattr(engine, "SOURCES", {})
    engine.sync(db(), [g], RuleDecisions())
    engine.sync(db(), [g], RuleDecisions())
    assert g.calls[1] >= {"m1", "m2", "news1"} and engine.SOURCES["gmail"]["count"] == 0

    _mark("gmail")
    engine.SOURCES["gmail"] = {"ok": False, "at": now.isoformat(), "error":
                               "<HttpError 403 ... returned \"Quota exceeded for quota metric 'Total Query Cost'\">"}
    src = {x["name"]: x for x in c.get("/api/state").json()["sources"]}["gmail"]
    assert src["error"] == "Gmail is limiting how fast Loops can read." and "next check" in src["fix"]
