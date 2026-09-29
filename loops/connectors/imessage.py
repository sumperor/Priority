"""iMessage and texts, read straight from the Messages database on this Mac. Nothing leaves the machine.
macOS protects that file, so the app running Loops (Terminal) needs Full Disk Access once."""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from ..models import Message

DB = os.path.expanduser(os.getenv("IMESSAGE_DB", "~/Library/Messages/chat.db"))
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
MAX_MESSAGES = 2000


def available():
    return sys.platform == "darwin" or bool(os.getenv("IMESSAGE_DB"))


def _open():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)


def can_read():
    """(ok, reason). Reason explains what to do when macOS blocks access."""
    if not available():
        return False, "iMessage is only available on a Mac."
    if not os.path.exists(DB):
        return False, "No Messages database found. Open the Messages app once and sign in."
    try:
        with _open() as c:
            c.execute("SELECT 1 FROM message LIMIT 1").fetchall()
        return True, ""
    except sqlite3.DatabaseError as e:
        return False, ("macOS is blocking access. Give Terminal Full Disk Access, then restart Sparrow."
                       if "unable to open" in str(e) or "authoriz" in str(e) else str(e))


def decode_body(blob):
    """Newer macOS keeps the text in 'attributedBody' (a serialised NSAttributedString), not 'text'."""
    if not blob:
        return ""
    try:
        b = bytes(blob).split(b"NSString", 1)[1][5:]
        if b[0] == 0x81:
            n, start = int.from_bytes(b[1:3], "little"), 3
        elif b[0] == 0x82:
            n, start = int.from_bytes(b[1:4], "little"), 4
        else:
            n, start = b[0], 1
        return b[start:start + n].decode("utf-8", "ignore")
    except Exception:
        return ""


def _ts(v):
    v = v or 0
    secs = v / 1e9 if v > 1e11 else v  # nanoseconds since 2001 on newer macOS, seconds on older
    return APPLE_EPOCH + timedelta(seconds=secs)


class IMessageConnector:
    name = "imessage"

    def authenticate(self):
        ok, why = can_read()
        print("iMessage connected." if ok else why)

    def fetch(self, since: datetime) -> list[Message]:
        ok, why = can_read()
        if not ok:
            raise RuntimeError(why)
        secs = (since - APPLE_EPOCH).total_seconds()
        q = """SELECT m.ROWID id, m.text, m.attributedBody body, m.date, m.is_from_me, h.id handle,
                      c.chat_identifier chat, c.display_name chat_name
               FROM message m
               LEFT JOIN handle h ON h.ROWID = m.handle_id
               LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
               LEFT JOIN chat c ON c.ROWID = j.chat_id
               WHERE m.date > ? OR (m.date < 100000000000 AND m.date > ?)
               ORDER BY m.date DESC LIMIT ?"""
        with _open() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(q, (secs * 1e9, secs, MAX_MESSAGES)).fetchall()
        out = []
        for r in rows:
            text = (r["text"] or decode_body(r["body"])).strip()
            ts = _ts(r["date"])
            if not text or ts < since:
                continue
            who = r["handle"] or "Unknown"
            out.append(Message(source="imessage", msg_id=str(r["id"]), thread_id=r["chat"] or who, sender=who,
                               sender_name=who, is_from_me=bool(r["is_from_me"]),
                               text=text[:4000], ts=ts, subject=r["chat_name"] or ""))
        return out
