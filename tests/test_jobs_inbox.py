"""Every job in an alert email is listed first; relevance is only checked when asked."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

ALERT_HTML = """<html><head><style>.x{}</style></head><body><table>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/111/?trk=a">Graduate Analyst</a></td></tr><tr><td>Acme Consulting</td></tr><tr><td>London</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/222/?trk=a">Strategy Associate</a></td></tr><tr><td>Beta &amp; Co</td></tr><tr><td>Manchester</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/333/">Data Intern</a></td></tr><tr><td>Gamma Ltd</td></tr><tr><td>Remote</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/alerts/unsubscribe">Unsubscribe</a></td></tr></table></body></html>"""


def test_html_only_alert_gives_every_job():
    from loops.connectors.gmail import html_text
    from loops.leads import parse_alert
    from loops.models import Message
    m = Message("gmail", "m1", "t1", "jobalerts-noreply@linkedin.com", "LinkedIn Job Alerts", False, html_text(ALERT_HTML),
                datetime.now(timezone.utc), "3 new jobs for graduate analyst")
    jobs = parse_alert(m)
    assert [j["title"] for j in jobs] == ["Graduate Analyst", "Strategy Associate", "Data Intern"]


def test_bookings_in_other_tabs_are_read_but_newsletters_are_not():
    from loops.connectors.gmail import ACTIONABLE
    assert ACTIONABLE.search("Your booking is confirmed: Careers Fair, Thu 2 Oct")
    assert ACTIONABLE.search("Reminder: your appointment tomorrow")
    assert not ACTIONABLE.search("This week's top 10 stories")


def test_list_first_then_check_relevance_on_request(tmp_path, monkeypatch):
    from loops import config as C
    monkeypatch.setattr(C, "DB_PATH", str(tmp_path / "j.db"))
    monkeypatch.setattr(C, "CONNECTORS", [])
    monkeypatch.setattr(C, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from loops import jobs
    from loops.connectors.gmail import html_text
    from loops.leads import scan
    from loops.models import Message
    from loops.server import _session_db, app
    c = TestClient(app)
    s = _session_db()
    now = datetime.now(timezone.utc)
    s.upsert_messages([Message("gmail", "m1", "t1", "jobalerts-noreply@linkedin.com", "LinkedIn Job Alerts", False,
                               html_text(ALERT_HTML), now, "3 new jobs for graduate analyst")])
    scan(s, datetime(2000, 1, 1, tzinfo=timezone.utc))
    J = c.get("/api/jobs").json()
    assert len(J["jobs"]) == 3 and J["emails"] == 1
    assert all(x["review"] is None for x in J["jobs"])                       # nothing judged yet

    c.post("/api/profile", json={"interests": "graduate consulting and strategy roles in London"})
    assert "what you're looking for" in jobs.profile(s)[1]
    monkeypatch.setattr(jobs, "_jd", lambda store, lead: "")               # no job-page fetching in tests
    c.post("/api/jobs/check-all")
    import time
    for _ in range(50):
        if not jobs.check_state["running"]:
            break
        time.sleep(0.05)
    J = c.get("/api/jobs").json()
    assert all(x["review"] for x in J["jobs"]) and jobs.check_state["done"] == 3


def test_tracking_links_and_unknown_senders_still_give_jobs():
    from loops.connectors.gmail import html_text
    from loops.leads import is_job_alert, parse_alert
    from loops.models import Message
    html = """<a href="https://u123.ct.sendgrid.net/ls/click?upn=aaa">Graduate Strategy Analyst</a><p>Northwind Energy</p><p>London</p>
    <a href="https://u123.ct.sendgrid.net/ls/click?upn=bbb">Software Engineer Intern</a><p>Acme</p><p>Remote</p>
    <a href="https://u123.ct.sendgrid.net/ls/click?upn=ccc">Privacy policy</a>
    <a href="https://u123.ct.sendgrid.net/ls/click?upn=ddd">View all jobs</a>"""
    m = Message("gmail", "t1", "t1", "digest@newjobsite.io", "NewJobSite", False, html_text(html),
                datetime.now(timezone.utc), "Graduate jobs for you this week")
    assert is_job_alert(m.sender, m.sender_name, m.subject)
    jobs = parse_alert(m)
    assert [j["title"] for j in jobs] == ["Graduate Strategy Analyst", "Software Engineer Intern"]
    assert jobs[0]["company"] == "Northwind Energy"
