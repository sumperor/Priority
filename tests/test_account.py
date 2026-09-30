"""Website pages and accounts: landing, pricing, login; sign up, log in, wrong password, log out."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_pages_and_accounts(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    from fastapi.testclient import TestClient
    from loops.server import app
    c = TestClient(app)
    w = c.get("/welcome")
    import re
    visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", w.text))
    assert w.status_code == 200 and "Start finishing." in visible and "£6" in w.text
    assert c.get("/static/vendor/gsap/gsap.min.js").status_code == 200
    assert c.get("/login").status_code == 200
    assert c.get("/pricing", follow_redirects=False).headers["location"] == "/welcome#pricing"
    assert c.get("/static/vendor/motion.js").status_code == 200

    assert c.post("/api/account/signup", json={"email": "bad", "password": "longenough"}).status_code == 400
    assert c.post("/api/account/signup", json={"email": "s@x.com", "password": "short"}).status_code == 400
    r = c.post("/api/account/signup", json={"name": "Sumedh", "email": "S@x.com", "password": "correct horse", "plan": "pro"})
    assert r.status_code == 200 and r.json()["user"] == {"email": "s@x.com", "name": "Sumedh", "plan": "free", "wanted": "pro"}
    assert c.get("/api/account").json()["user"]["name"] == "Sumedh"
    assert c.post("/api/account/signup", json={"email": "s@x.com", "password": "another one"}).status_code == 400
    c.post("/api/account/logout")
    assert c.get("/api/account").json()["user"] is None
    assert c.post("/api/account/login", json={"email": "s@x.com", "password": "wrong password"}).status_code == 400
    assert c.post("/api/account/login", json={"email": "s@x.com", "password": "correct horse"}).json()["user"]["name"] == "Sumedh"
    raw = open(tmp_path / "secrets" / "accounts.json").read()
    assert "correct horse" not in raw   # never stored as typed
