"""Local SQLite store: messages, loops, and a log of every decision (for calibration)."""
import sqlite3
from datetime import datetime, timezone

from .models import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  source TEXT, msg_id TEXT, thread_id TEXT, sender TEXT, sender_name TEXT,
  is_from_me INTEGER, subject TEXT, text TEXT, ts TEXT,
  PRIMARY KEY(source, msg_id));
CREATE INDEX IF NOT EXISTS ix_thread ON messages(source, thread_id, ts);
CREATE TABLE IF NOT EXISTS loops(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, thread_id TEXT, type TEXT, person TEXT, summary TEXT, done_when TEXT,
  due TEXT, cost INTEGER, consequence TEXT, reversible INTEGER, hard_deadline INTEGER,
  effort_h REAL, stakes TEXT, blocks INTEGER, mattered TEXT,
  status TEXT DEFAULT 'open',          -- open | pending_close | closed
  close_confidence REAL, outcome TEXT, actual_h REAL,
  trigger_ts TEXT, created TEXT, closed_at TEXT, on_time INTEGER, snoozes INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS checks(key TEXT PRIMARY KEY, ts TEXT);
CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY AUTOINCREMENT, loop_id INTEGER, role TEXT, text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, loop_id INTEGER, question TEXT,
  p REAL, backend TEXT, ts TEXT, confirmed INTEGER, kind TEXT);
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, msg_id TEXT, title TEXT, company TEXT, location TEXT,
  url TEXT, snippet TEXT, credible TEXT, flags TEXT, fit INTEGER, verdict TEXT, reasons TEXT,
  status TEXT DEFAULT 'new', loop_id INTEGER, created TEXT, UNIQUE(source, msg_id, url));
CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, loop_id INTEGER, kind TEXT, body TEXT, created TEXT);
"""

def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after the first release to existing databases."""
        for table, col, typ in [("loops", "blocks", "INTEGER"), ("loops", "mattered", "TEXT"),
                                ("loops", "trigger_ts", "TEXT"), ("decisions", "kind", "TEXT"),
                                ("loops", "note", "TEXT"), ("loops", "started_at", "TEXT"),
                                ("loops", "commit_at", "TEXT"), ("loops", "last_nudge", "TEXT"),
                                ("loops", "area", "TEXT"), ("loops", "evidence", "TEXT"),
                                ("loops", "acked", "INTEGER")]:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # ---------- messages ----------
    def messages_since(self, since: datetime, incoming_only=True):
        q = "SELECT * FROM messages WHERE ts >= ?" + (" AND is_from_me=0" if incoming_only else "") + " ORDER BY ts"
        return [Message(r["source"], r["msg_id"], r["thread_id"], r["sender"], r["sender_name"],
                        bool(r["is_from_me"]), r["text"], datetime.fromisoformat(r["ts"]), r["subject"])
                for r in self.db.execute(q, (since.isoformat(),))]

    def known_ids(self, source):
        """Messages already downloaded (or looked at and skipped), so a sync only fetches new ones."""
        ids = {r[0] for r in self.db.execute("SELECT msg_id FROM messages WHERE source=?", (source,))}
        p = f"seen:{source}:"
        ids |= {r[0][len(p):] for r in self.db.execute("SELECT key FROM checks WHERE key LIKE ?", (p + "%",))}
        return ids

    def upsert_messages(self, msgs: list[Message]):
        self.db.executemany(
            "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
            [(m.source, m.msg_id, m.thread_id, m.sender, m.sender_name, int(m.is_from_me),
              m.subject, m.text, m.ts.isoformat()) for m in msgs])
        self.db.commit()

    def recent_threads(self, since: datetime):
        rows = self.db.execute(
            "SELECT DISTINCT source, thread_id FROM messages WHERE ts >= ?", (since.isoformat(),))
        return [(r["source"], r["thread_id"]) for r in rows]

    def thread_messages(self, source, thread_id):
        rows = self.db.execute(
            "SELECT * FROM messages WHERE source=? AND thread_id=? ORDER BY ts", (source, thread_id))
        return [Message(r["source"], r["msg_id"], r["thread_id"], r["sender"], r["sender_name"],
                        bool(r["is_from_me"]), r["text"], datetime.fromisoformat(r["ts"]), r["subject"])
                for r in rows]

    # ---------- idempotency: don't re-ask the same question ----------
    def checked(self, key):
        return self.db.execute("SELECT 1 FROM checks WHERE key=?", (key,)).fetchone() is not None

    def mark_checked(self, key):
        self.db.execute("INSERT OR IGNORE INTO checks VALUES(?,?)", (key, now_iso()))
        self.db.commit()

    # ---------- loops ----------
    def open_loop(self, source, thread_id, type_):
        return self.db.execute(
            "SELECT * FROM loops WHERE source=? AND thread_id=? AND type=? AND status!='closed'",
            (source, thread_id, type_)).fetchone()

    def create_loop(self, **f):
        from .areas import classify
        f.setdefault("created", now_iso())
        f.setdefault("area", classify(f.get("summary", ""), f.get("stakes", ""), f.get("person", ""),
                                      f.get("source", ""), f.get("type", "")))
        cols = ",".join(f)
        cur = self.db.execute(f"INSERT INTO loops({cols}) VALUES({','.join('?' * len(f))})", tuple(f.values()))
        self.db.commit()
        return cur.lastrowid

    def update_loop(self, loop_id, **f):
        sets = ",".join(f"{k}=?" for k in f)
        self.db.execute(f"UPDATE loops SET {sets} WHERE id=?", (*f.values(), loop_id))
        self.db.commit()

    def get_loop(self, loop_id):
        return self.db.execute("SELECT * FROM loops WHERE id=?", (loop_id,)).fetchone()

    def loops(self, status=None):
        if status:
            return self.db.execute("SELECT * FROM loops WHERE status=?", (status,)).fetchall()
        return self.db.execute("SELECT * FROM loops WHERE status!='closed'").fetchall()

    def close_loop(self, loop_id, outcome, confidence=None, actual_h=None):
        loop = self.get_loop(loop_id)
        closed = datetime.now(timezone.utc)
        # Dismissed loops weren't real tasks, so they don't count toward the on-time rate
        on_time = (int(closed <= datetime.fromisoformat(loop["due"]))
                   if loop["due"] and outcome not in ("dismissed", "dropped") else None)
        self.update_loop(loop_id, status="closed", outcome=outcome, closed_at=closed.isoformat(),
                         on_time=on_time, close_confidence=confidence, actual_h=actual_h)

    # ---------- chasing conversation ----------
    def add_chat(self, loop_id, role, text):
        cur = self.db.execute("INSERT INTO chat(loop_id,role,text,ts) VALUES(?,?,?,?)", (loop_id, role, text, now_iso()))
        self.db.commit()
        return cur.lastrowid

    def chat(self, loop_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM chat WHERE loop_id=? ORDER BY id", (loop_id,))]

    def nudge_count(self, loop_id):
        return self.db.execute("SELECT COUNT(*) n FROM chat WHERE loop_id=? AND role='agent'", (loop_id,)).fetchone()["n"]

    # ---------- learning from history ----------
    def on_time_rate(self, type_):
        r = self.db.execute(
            "SELECT COUNT(*) n, SUM(on_time) k FROM loops WHERE status='closed' AND type=? AND on_time IS NOT NULL",
            (type_,)).fetchone()
        return (r["k"] or 0), r["n"]

    def effort_multiplier(self, type_):
        rows = self.db.execute(
            "SELECT actual_h/effort_h r FROM loops WHERE type=? AND actual_h>0 AND effort_h>0", (type_,)).fetchall()
        ratios = sorted(r["r"] for r in rows)
        if len(ratios) < 5:
            return 1.0
        return ratios[len(ratios) // 2]  # median

    def log_decision(self, loop_id, question, p, backend, kind="detect"):
        cur = self.db.execute("INSERT INTO decisions(loop_id,question,p,backend,ts,kind) VALUES(?,?,?,?,?,?)",
                              (loop_id, question, p, backend, now_iso(), kind))
        self.db.commit()
        return cur.lastrowid

    def label_decision(self, loop_id, kind, correct: bool):
        """Record whether the latest decision of this kind turned out right (feeds accuracy stats)."""
        self.db.execute("UPDATE decisions SET confirmed=? WHERE id=(SELECT MAX(id) FROM decisions "
                        "WHERE loop_id=? AND kind=?)", (int(correct), loop_id, kind))
        self.db.commit()

    def closed_loops(self, limit=40):
        return self.db.execute("SELECT * FROM loops WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
                               (limit,)).fetchall()

    def importance_multiplier(self, consequence):
        """Learned from 'did it matter more/less than expected?' feedback. Needs 5+ answers."""
        rows = self.db.execute("SELECT mattered FROM loops WHERE consequence=? AND mattered IS NOT NULL",
                               (consequence,)).fetchall()
        if len(rows) < 5:
            return 1.0
        f = {"more": 1.3, "as_expected": 1.0, "less": 0.7}
        return sum(f.get(r["mattered"], 1.0) for r in rows) / len(rows)

    def accuracy(self):
        """Honest numbers with sample sizes, so small samples aren't overtrusted."""
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        r = self.db.execute("SELECT COUNT(*) n, SUM(on_time) k FROM loops WHERE status='closed' "
                            "AND on_time IS NOT NULL AND closed_at>=?", (since,)).fetchone()
        out = {"on_time_14d": {"k": r["k"] or 0, "n": r["n"]}}
        for kind in ("detect", "close"):
            d = self.db.execute("SELECT p, confirmed FROM decisions WHERE kind=? AND confirmed IS NOT NULL",
                                (kind,)).fetchall()
            n = len(d)
            out[kind] = {"n": n,
                         "correct": sum(1 for x in d if (x["p"] >= 0.5) == bool(x["confirmed"])),
                         "brier": round(sum((x["p"] - x["confirmed"]) ** 2 for x in d) / n, 3) if n else None}
        return out
