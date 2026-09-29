"""Phone calls happen only when a deadline is close and reminders are ignored, and never mid-lecture."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

NOW = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)   # mid-afternoon, not quiet hours


def setup(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    from loops import caller
    caller.save({"enabled": True, "phone": "+447700900123", "agent_id": "agent_1", "phone_number_id": "pn_1",
                 "quiet_from": 23, "quiet_to": 7})
    from loops.store import Store
    s = Store(C.DB_PATH)
    i = s.create_loop(source="manual", thread_id="", type="task", person="", summary="Complete the Shell test", done_when="",
                      due=(NOW + timedelta(minutes=50)).isoformat(), cost=85, consequence="opportunity", reversible=0,
                      hard_deadline=1, effort_h=1.0, stakes="The application ends there.")
    loop = {**dict(s.get_loop(i)), "forecast": {"latest_start": (NOW - timedelta(minutes=10)).isoformat()}, "effort_adj": 1.0}
    return s, loop, caller


def nudge(s, loop, mins_ago):
    s.db.execute("INSERT INTO chat(loop_id,role,text,ts) VALUES(?,?,?,?)",
                 (loop["id"], "agent", "Are you on it?", (NOW - timedelta(minutes=mins_ago)).isoformat()))
    s.db.commit()


def test_only_calls_when_close_and_ignored(tmp_path, monkeypatch):
    s, loop, caller = setup(tmp_path, monkeypatch)
    assert caller.why_not(s, loop, NOW, [], False) == "reminders not ignored yet"
    nudge(s, loop, 12)
    nudge(s, loop, 2)
    assert caller.why_not(s, loop, NOW, [], False) == "reminders not ignored yet"      # latest one only 2 min ago
    nudge(s, loop, 7)
    s.db.execute("DELETE FROM chat WHERE ts > ?", ((NOW - timedelta(minutes=5)).isoformat(),)); s.db.commit()
    assert caller.why_not(s, loop, NOW, [], False) is None

    lecture = [{"title": "Econometrics lecture", "start": (NOW - timedelta(minutes=30)).isoformat(),
                "end": (NOW + timedelta(minutes=30)).isoformat()}]
    assert caller.why_not(s, loop, NOW, lecture, False) == "busy: Econometrics lecture"
    assert caller.why_not(s, loop, NOW, [], True) == "in a focus session"
    early = {**loop, "forecast": {"latest_start": (NOW + timedelta(hours=2)).isoformat()}}
    assert caller.why_not(s, early, NOW, [], False) == "deadline not close yet"

    # you replied: no call
    s.db.execute("INSERT INTO chat(loop_id,role,text,ts) VALUES(?,?,?,?)", (loop["id"], "you", "on it", NOW.isoformat())); s.db.commit()
    assert caller.why_not(s, loop, NOW, [], False) == "reminders not ignored yet"


def test_call_passes_real_numbers_and_acts_on_the_answer(tmp_path, monkeypatch):
    s, loop, caller = setup(tmp_path, monkeypatch)
    for m in (15, 8):
        nudge(s, loop, m)
    sent = {}

    class R:
        def __init__(self, data, ok=True): self._d, self.ok, self.status_code, self.text = data, ok, 200, ""
        def json(self): return self._d
        def raise_for_status(self): pass

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return R({"success": True, "conversation_id": "conv_1", "callSid": "CA1"})
    monkeypatch.setattr(caller.requests, "post", post)
    assert caller.tick(s, [loop], [], False, NOW) == "conv_1"
    v = sent["body"]["conversation_initiation_client_data"]["dynamic_variables"]
    assert sent["url"].endswith("/twilio/outbound-call") and sent["body"]["to_number"] == "+447700900123"
    assert v["minutes_left"] == "50" and v["minutes_needed"] == "60" and v["short_by"] == "10"
    assert caller.tick(s, [loop], [], False, NOW) is None                                # only one call per task

    monkeypatch.setattr(caller.requests, "get", lambda url, headers=None, timeout=None: R(
        {"status": "done", "transcript": [{"role": "agent", "message": "When will you start?"},
                                          {"role": "user", "message": "I'll start in 10 minutes"}]}))
    caller.follow_up(s)
    chat = [r["text"] for r in s.db.execute("SELECT text FROM chat WHERE loop_id=? ORDER BY id", (loop["id"],))]
    assert "I'll start in 10 minutes" in chat and s.get_loop(loop["id"])["commit_at"]


def test_quiet_hours(tmp_path, monkeypatch):
    s, loop, caller = setup(tmp_path, monkeypatch)
    st = caller.settings()
    night = datetime.now().astimezone().replace(hour=23, minute=30).astimezone(timezone.utc)
    assert caller._quiet(st, night) and not caller._quiet(st, NOW)
