"""Jobs from the inbox: scan (including old alerts), judge against CV or LinkedIn, tailored CV in PDF/Word/text."""
import io
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loops.models import Message

NOW = datetime.now(timezone.utc)
CV = """Sumedh Garimella
sumedh@example.com | London | linkedin.com/in/sumedh
EDUCATION
BSc Economics, University of London, 2025
EXPERIENCE
Data Analyst Intern, Acme Bank, 2024
Built Python and SQL dashboards tracking loan defaults for the risk team
Cleaned 2 million rows of transaction data in pandas
SKILLS
Python, SQL, Excel, Tableau, statistics"""
ALERT = ("Graduate Data Analyst\nMonzo\nLondon\nView job: https://www.linkedin.com/jobs/view/111/\n\n"
         "Junior Brand Designer\nCanva\nLondon\nhttps://www.linkedin.com/jobs/view/222/\n")


def app(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "j.db"))
    monkeypatch.setattr(C, "CONNECTORS", ["gmail"])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import loops.session as sess
    monkeypatch.setattr(sess, "_page_text", lambda url: "")      # job sites unreachable in tests
    from fastapi.testclient import TestClient
    from loops import connect
    from loops.server import app as a
    monkeypatch.setattr(connect, "after_connect", None)
    connect._mark("gmail")
    return TestClient(a)


def test_scan_judge_and_tailor(tmp_path, monkeypatch):
    c = app(tmp_path, monkeypatch)
    old = Message("gmail", "old1", "old1", "jobalerts-noreply@linkedin.com", "LinkedIn Job Alerts", False, ALERT,
                  NOW - timedelta(days=9), "Data analyst: 2 new jobs")

    class FakeGmail:
        name = "gmail"
        def fetch(self, since, known=frozenset()): return []
        def fetch_jobs(self, since, known=frozenset()):
            assert since < NOW - timedelta(days=13)                    # looks back 14 days
            return [old]
    from loops import connectors
    monkeypatch.setitem(connectors.ALL, "gmail", FakeGmail)

    assert c.get("/api/profile").json()["cv"] == ""
    c.post("/api/docs/cv", json={"name": "CV.pdf", "text": CV})
    c.post("/api/jobs/scan", json={"days": 14})
    for _ in range(50):
        if not c.get("/api/jobs").json()["scan"]["running"]:
            break
        time.sleep(0.05)
    jobs = c.get("/api/jobs").json()["jobs"]
    assert {j["title"] for j in jobs} == {"Graduate Data Analyst", "Junior Brand Designer"}
    assert jobs[0]["origin"]["from"] == "LinkedIn Job Alerts"

    da = next(j for j in jobs if j["title"] == "Graduate Data Analyst")
    de = next(j for j in jobs if j["title"] == "Junior Brand Designer")
    r1 = c.post(f"/api/leads/{da['id']}/review").json()
    r2 = c.post(f"/api/leads/{de['id']}/review").json()
    assert r1["verdict"] in ("apply", "maybe") and r2["verdict"] == "skip"
    assert r1["compared_with"].startswith("your CV") and r1["checked_by"] == "rules"
    assert c.get("/api/jobs").json()["jobs"][0]["title"] == "Graduate Data Analyst"   # best first

    # compare with LinkedIn instead
    c.post("/api/docs/linkedin", json={"name": "Profile.pdf", "text": "Sumedh Garimella\nBrand designer. Figma, illustration, "
                                                                        "visual identity, typography, Adobe Creative Suite. " * 3})
    c.post("/api/profile", json={"compare_with": "linkedin"})
    assert c.post(f"/api/leads/{de['id']}/review").json()["compared_with"] == "your LinkedIn profile"
    c.post("/api/profile", json={"compare_with": "cv"})

    cv = c.post(f"/api/leads/{da['id']}/cv").json()
    assert cv["name"] == "Sumedh Garimella" and "Acme Bank" in cv["text"] and cv["format"] in ("pdf", "word", "form", "any")
    assert cv["for"] == "Graduate Data Analyst at Monzo"
    page = c.get(f"/cv/{cv['id']}").text
    assert "Acme Bank" in page and "Save as PDF" in page
    w = c.get(f"/cv/{cv['id']}.docx")
    assert w.headers["content-type"].startswith("application/vnd.openxmlformats")
    doc = zipfile.ZipFile(io.BytesIO(w.content)).read("word/document.xml").decode()
    assert "Acme Bank" in doc and "Sumedh Garimella" in doc


def test_no_profile_and_no_inbox(tmp_path, monkeypatch):
    c = app(tmp_path, monkeypatch)
    from loops import connect
    connect._mark("gmail", False)
    assert c.post("/api/jobs/scan", json={"days": 14}).status_code == 400
    assert c.post("/api/profile/linkedin-url", json={"url": "https://example.com/me"}).status_code == 400
    assert "API key" in c.post("/api/profile/linkedin-url", json={"url": "https://www.linkedin.com/in/sumedh"}).json()["detail"]


def test_application_process_and_format_from_job_text():
    from loops.jobs import _format, rule_review
    jd = ("Graduate Analyst. Apply with your CV in PDF format. Stages: online assessment (numerical reasoning), "
          "HireVue video interview, then an assessment centre. Closing date: 12 October 2026. A cover letter is required.")
    lead = {"title": "Graduate Analyst", "snippet": "", "credible": "credible", "url": ""}
    r = rule_review(lead, jd, CV)
    assert r["process"]["steps"] == ["Online tests", "Video interview", "Assessment centre or final round"]
    assert r["process"]["deadline"] == "12 October 2026" and r["process"]["cover_letter"] and r["process"]["cv_format"] == "pdf"
    assert _format("Upload your CV as a Word document (.docx)") == "word"
    assert _format("Please complete our application form. No CVs.") == "form"
