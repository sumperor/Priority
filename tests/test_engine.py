"""Offline test of the whole loop with fake connectors and a fake decision engine."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.engine import ranked, sync
from loops.models import Message
from loops.store import Store

NOW = datetime.now(timezone.utc)


class FakeConnector:
    name = "fake"
    def __init__(self, msgs): self.msgs = msgs
    def fetch(self, since): return self.msgs


class FakeDecisions:
    name = "fake"
    def yes_no(self, state, question):
        last = state.strip().splitlines()[-1].lower()
        if "need a reply" in question: return 0.9 if "?" in last else 0.1
        if "commit" in question: return 0.9 if "i'll" in last else 0.1
        if "waiting on" in question: return 0.9 if "?" in last else 0.1
        if "complete" in question: return 0.95 if "attached" in state.lower() else 0.2
        if "deliver" in question: return 0.7
        return 0.5


def fake_extract(state, loop_type):
    return {"summary": f"{loop_type} task", "person": "Sam", "done_when": "reply sent", "due": None,
            "cost": 80 if loop_type == "promise" else 30, "consequence": "opportunity",
            "reversible": loop_type != "promise", "hard_deadline": False,
            "effort_h": 0.1, "stakes": "Sam moves on."}


def m(src, tid, mid, me, text, hours_ago):
    return Message(src, mid, tid, "me@x.com" if me else "sam@x.com", "Me" if me else "Sam",
                   me, text, NOW - timedelta(hours=hours_ago))


def test_full_cycle(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    d = FakeDecisions()
    msgs = [
        m("gmail", "t1", "1", False, "Can you send the deck by Friday?", 5),              # reply owed
        m("slack", "t2", "2", True, "I'll send the numbers tomorrow", 3),                 # promise
        m("outlook", "t3", "3", True, "Could you confirm the start date?", 24 * 5),       # waiting
        m("gmail", "t4", "4", False, "Thanks, all good.", 2),                              # nothing
    ]
    new, _ = sync(store, [FakeConnector(msgs)], d, fake_extract)
    types = sorted(store.get_loop(i)["type"] for i in new)
    assert types == ["promise", "reply", "waiting"], types

    # Re-sync with no new messages must not duplicate or re-ask
    new2, _ = sync(store, [FakeConnector(msgs)], d, fake_extract)
    assert new2 == []

    # I reply with the deck: reply loop should auto-close
    msgs.append(m("gmail", "t1", "5", True, "Deck attached.", 0))
    _, closed = sync(store, [FakeConnector(msgs)], d, fake_extract)
    assert any(r[1] == "closed" for r in closed)

    # Irreversible, high-cost promise ranks first
    top = ranked(store)[0]
    assert top["type"] == "promise" and top["tier"] == 0
