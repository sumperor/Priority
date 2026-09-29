"""Job-hunt session: intake -> objectives -> applications -> check-ins -> end creates follow-ups."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_session_flow(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)

    assert c.get("/api/session").json()["active"] is None
    qs = c.get("/api/session/intake").json()["questions"]
    assert qs[0]["id"] == "kind"
    r = c.post("/api/session/start", json={"answers": {"kind": "apply", "target": "3", "roles": "graduate data analyst roles",
                                                         "minutes": "60", "rhythm": "25/5", "research": "0"}})
    assert r.status_code == 200
    s = c.get("/api/session").json()
    assert s["active"]["title"] == "Apply to 3 graduate data analyst roles" and s["active"]["focus_min"] == 25

    # Nudges are paused during a session
    assert c.post("/api/nudges/tick").json()["nudges"] == []

    jd = "Graduate Data Analyst\nJoin Acme Analytics, a London fintech. You will build dashboards in SQL and Python. " * 10
    a = c.post("/api/apps", json={"input": jd}).json()
    assert a["company"] == "Acme Analytics"
    c.post(f"/api/apps/{a['id']}", json={"status": "submitted"})
    s = c.get("/api/session").json()
    assert s["submitted"] == 1 and "2 to go" in s["pace"]

    ck = c.post("/api/session/checkin", json={"mood": "good", "focus_minutes": 70}).json()
    assert "water" in ck["message"]
    ck = c.post("/api/session/checkin", json={"mood": "tired", "focus_minutes": 10}).json()
    assert ck["break_min"] >= 15

    e = c.post("/api/session/end").json()
    assert "1 application submitted" in e["summary"]
    loops = c.get("/api/state").json()["loops"]
    assert any(l["type"] == "waiting" and "Acme Analytics" in l["summary"] for l in loops)
