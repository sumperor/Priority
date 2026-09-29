"""API test: capture, forecast, feedback, accuracy. No accounts or API keys needed."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_app_flow(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    import loops.extract as ex

    def fake_extract(state, t):
        hard = "application" in state
        return {"summary": state.split(": ", 1)[1][:60], "person": "", "done_when": "", "due": None,
                "cost": 90 if hard else 20, "consequence": "opportunity" if hard else "minor",
                "reversible": not hard, "hard_deadline": hard, "effort_h": 2 if hard else 0.1, "stakes": ""}
    monkeypatch.setattr(ex, "extract", fake_extract)

    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)

    a = c.post("/api/capture", json={"text": "Submit the application"}).json()["id"]
    b = c.post("/api/capture", json={"text": "Reply to Sam"}).json()["id"]
    d = c.post("/api/capture", json={"text": "Book interview travel"}).json()["id"]

    # Application holds up the travel booking
    assert c.patch(f"/api/loops/{a}", json={"blocks": d}).status_code == 200
    s = c.get("/api/state").json()
    top = next(l for l in s["loops"] if l["id"] == a)
    assert top["tier"] == 0 and top["bucket"] == "Do now"        # irreversible, high cost is pressing
    first = s["loops"][0]
    assert first["id"] == a or first["why"].startswith("Quick one first")  # quick tasks may go first
    assert "Book interview travel" in top["forecast"]["if_missed"]  # knock-on shown
    assert "lost for good" in top["forecast"]["slip_1d"]

    # Overdue loop is flagged as missed
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    c.patch(f"/api/loops/{b}", json={"due": past})
    fb = next(l for l in c.get("/api/state").json()["loops"] if l["id"] == b)["forecast"]
    assert fb["level"] == "late" and fb["headline"].startswith("Missed")

    # Feedback flows into accuracy
    c.post(f"/api/loops/{b}/close", json={"outcome": "well", "mattered": "less", "actual_h": 0.2})
    c.post(f"/api/loops/{d}/dismiss")
    acc = c.get("/api/state").json()["accuracy"]
    assert acc["on_time_14d"]["n"] == 1 and acc["on_time_14d"]["k"] == 0
    assert c.get("/").status_code == 200
