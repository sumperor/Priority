"""Reminders reach the Mac and phone from the server, not just the open tab."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_reminders_go_to_phone_and_mac(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "n.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from loops import notify
    sent = []
    monkeypatch.setattr(notify, "mac_available", lambda: True)
    monkeypatch.setattr(notify, "send_mac", lambda t, b, url=None: sent.append(("mac", b, url)) or "osascript")
    monkeypatch.setattr(notify, "send_ntfy", lambda topic, t, b, tags="": sent.append(("ntfy", topic, b)))
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)

    st = c.post("/api/notify", json={"mac": True, "ntfy": True}).json()
    assert st["ntfy"] and st["ntfy_topic"].startswith("sparrow-") and len(st["ntfy_topic"]) > 14
    assert c.post("/api/notify/test").json()["sent"] == ["osascript", "ntfy"]
    sent.clear()

    i = c.post("/api/capture", json={"text": "Prepare for my interview, it takes an hour"}).json()["id"]
    c.patch(f"/api/loops/{i}", json={"due": (datetime.now(timezone.utc) + timedelta(minutes=65)).isoformat()})
    n = c.post("/api/nudges/tick").json()["nudges"]
    assert n and n[0]["sent"] == ["osascript", "ntfy"]
    assert sent[0][0] == "mac" and sent[0][2].endswith(f"#task-{i}") and sent[1][1] == st["ntfy_topic"]

    c.post("/api/notify", json={"mac": False, "ntfy": False})
    assert c.post("/api/notify/test").status_code == 400
