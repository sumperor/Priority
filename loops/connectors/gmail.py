"""Gmail: read-only. Personal use runs as an unverified 'testing' app (you as a test user)."""
import base64
import html
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr

from ..config import GMAIL_CREDENTIALS, GMAIL_TOKEN
from ..leads import is_job_alert
from ..models import Message

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]
MAX_MESSAGES = 500
_BOARDS = ["linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.uk", "totaljobs.com", "reed.co.uk", "cv-library.co.uk",
           "gradcracker.com", "brightnetwork.co.uk", "targetjobs.co.uk", "prospects.ac.uk", "milkround.com", "otta.com",
           "welcometothejungle.com", "efinancialcareers.com", "ratemyplacement.co.uk", "joinhandshake.com"]
JOB_QUERY = ("{from:(" + " OR ".join(_BOARDS) + ") "
             'subject:("job alert" OR "jobs for you" OR "new jobs" OR "is hiring" OR "recommended jobs" OR "jobs matching" OR '
             '"new roles" OR "vacancies" OR "graduate jobs" OR "internships" OR "opportunities for you")}')
# Bulk mail (it has an unsubscribe link) is only read when it looks like something you have to act on:
# bookings, tickets, orders, appointments, payments, deadlines, interviews, assessments.
ACTIONABLE = re.compile(r"\b(book(ing|ed)|confirm(ed|ation)|reservation|ticket|your order|order (number|#)|dispatched|delivery|"
                        r"appointment|reminder|invoice|payment|bill (is|due)|renew|expir(es|ing)|deadline|due (on|by|date)|"
                        r"action (required|needed)|verify|registration|registered|rsvp|invitation|invite|event|webinar|"
                        r"interview|assessment|application|offer|shortlist|next steps?|complete your|reset your password)\b", re.I)


def _part(payload, mime):
    """Depth-first search for the first part of this type."""
    if payload.get("mimeType") == mime and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        t = _part(part, mime)
        if t:
            return t
    return ""


def html_text(h):
    """HTML email to plain lines, keeping links as 'text url' so every job in an alert can be found."""
    h = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
               lambda m: f"\n{re.sub('<[^>]+>', ' ', m.group(2)).strip()} {m.group(1)}\n", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|td)>", "\n", h)
    h = html.unescape(re.sub(r"<[^>]+>", " ", h))
    lines = [re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in h.splitlines()]
    return "\n".join(l for l in lines if l)


def _body(payload) -> str:
    """The plain-text part, or the HTML part turned into text when there's no plain version."""
    t = _part(payload, "text/plain")
    if t.strip():
        return t
    h = _part(payload, "text/html")
    return html_text(h) if h else ""


QUOTE_START = re.compile(r"^(on .{6,120} wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+\s+sent:\s|_{8,}|"
                         r"sent from my (iphone|ipad|android)|get outlook for)", re.I)


def clean_body(text):
    """The new part of an email: no quoted earlier messages, no signature, no runs of blank lines."""
    out = []
    for line in (text or "").splitlines():
        t = line.strip()
        if QUOTE_START.match(t) or t == "--":
            break
        if t.startswith(">"):
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def parse_ics(ics):
    """SUMMARY, start, end, LOCATION and a join URL from a calendar attachment."""
    if not ics:
        return None
    lines = re.sub(r"\r?\n[ \t]", "", ics).splitlines()        # unfold long lines
    ev, inside = {}, False
    for ln in lines:
        if ln.startswith("BEGIN:VEVENT"):
            inside = True
        elif ln.startswith("END:VEVENT"):
            break
        elif inside and ":" in ln:
            k, v = ln.split(":", 1)
            ev[k.split(";")[0].upper()] = (k, v.replace("\\n", " ").replace("\\,", ",").strip())

    def when(key):
        if key not in ev:
            return None
        k, v = ev[key]
        tz = re.search(r"TZID=([^;:]+)", k)
        try:
            if len(v) == 8:
                return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
            d = datetime.strptime(v.rstrip("Z"), "%Y%m%dT%H%M%S")
            if v.endswith("Z"):
                return d.replace(tzinfo=timezone.utc)
            if tz:
                from zoneinfo import ZoneInfo
                return d.replace(tzinfo=ZoneInfo(tz.group(1))).astimezone(timezone.utc)
            return d.astimezone(timezone.utc)
        except Exception:
            return None
    start = when("DTSTART")
    if not start:
        return None
    desc = ev.get("DESCRIPTION", ("", ""))[1] + " " + ev.get("URL", ("", ""))[1] + " " + ev.get("LOCATION", ("", ""))[1]
    join = re.search(r"https?://\S*(zoom\.us/j/|meet\.google\.com/|teams\.microsoft\.com/l/meetup|webex\.com)\S*", desc)
    return {"title": ev.get("SUMMARY", ("", ""))[1], "start": start, "end": when("DTEND"),
            "place": ev.get("LOCATION", ("", ""))[1][:80], "join": join.group(0).rstrip(".,>") if join else ""}


def event_line(ev):
    """A calendar attachment written as one plain line at the top of the email, for Claude and the date rules alike."""
    if not ev:
        return ""
    s, e = ev["start"].astimezone(), (ev["end"] or ev["start"]).astimezone()
    return (f"Calendar event: {ev['title']}, {s:%A} {s.day} {s:%B} {s.year} at {s:%H:%M} to {e:%H:%M}"
            + (f". Place: {ev['place']}" if ev["place"] else "") + (f". Join {ev['join']}" if ev["join"] else "")
            + f". (start {ev['start'].isoformat()}, end {(ev['end'] or ev['start']).isoformat()})")


class GmailConnector:
    name = "gmail"

    def _creds(self, interactive=False):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN, SCOPES) if os.path.exists(GMAIL_TOKEN) else None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError("not authorised: connect it again from the app")
            creds = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, SCOPES).run_local_server(port=0)
        os.makedirs(os.path.dirname(GMAIL_TOKEN) or ".", exist_ok=True)
        with open(GMAIL_TOKEN, "w") as f:
            f.write(creds.to_json())
        return creds

    def authenticate(self):
        self._creds(interactive=True)
        print("Gmail connected.")

    def fetch(self, since: datetime, known=frozenset()) -> list[Message]:
        """Only downloads emails it hasn't seen before, and backs off if Gmail says slow down."""
        from googleapiclient.discovery import build

        self.note, self.skipped = "", []
        svc = build("gmail", "v1", credentials=self._creds(), cache_discovery=False)
        me = svc.users().getProfile(userId="me").execute(num_retries=2)["emailAddress"].lower()
        from ..config import write_secret
        write_secret("gmail_account.json", {"email": me})  # so "Open in Gmail" opens the right account
        # All Mail, every tab (Primary, Promotions, Social, Updates, Forums); Gmail leaves out spam and bin
        ids = self._list(svc, f"after:{int(since.timestamp())}", known)
        return self._get(svc, me, ids)

    def fetch_jobs(self, since, known=frozenset()):
        """Every job email in the window, whichever tab it landed in."""
        from googleapiclient.discovery import build
        self.note, self.skipped = "", []
        svc = build("gmail", "v1", credentials=self._creds(), cache_discovery=False)
        me = svc.users().getProfile(userId="me").execute(num_retries=2)["emailAddress"].lower()
        # one OR group: a job board sender, an alert subject, or a subject about jobs
        q = (f"after:{int(since.timestamp())} " + JOB_QUERY[:-1] + " subject:(job OR jobs OR role OR roles OR hiring OR "
             "vacancy OR vacancies OR internship OR internships OR graduate OR placement OR opportunity OR opportunities OR "
             "position OR positions OR careers OR apply OR scheme)}")
        return self._get(svc, me, self._list(svc, q, known, 500))

    def _list(self, svc, q, known, cap=MAX_MESSAGES):
        ids, page = [], None
        while len(ids) < cap:
            r = svc.users().messages().list(userId="me", q=q, pageToken=page, maxResults=100).execute(num_retries=2)
            ids += [m["id"] for m in r.get("messages", []) if m["id"] not in known]
            page = r.get("nextPageToken")
            if not page:
                break
        return ids[:cap]

    def _get(self, svc, me, ids):
        from googleapiclient.errors import HttpError

        out = []
        for i, mid in enumerate(ids[:MAX_MESSAGES]):
            try:
                m = svc.users().messages().get(userId="me", id=mid, format="full").execute(num_retries=2)
            except HttpError as e:
                if e.resp.status in (403, 429):
                    # Keep what we have; the rest comes on the next check
                    self.note = f"Gmail asked Sparrow to slow down. Got {i} new emails; the rest come on the next check."
                    break
                raise
            time.sleep(0.05)  # stay well under Gmail's per-second limit
            h = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
            name, addr = parseaddr(h.get("from", ""))
            addr = addr.lower()
            bulk = "list-unsubscribe" in h and addr != me and not is_job_alert(addr, name, h.get("subject", "")) \
                and not ACTIONABLE.search(f"{h.get('subject', '')} {m.get('snippet', '')}")
            if bulk and not os.getenv("ANTHROPIC_API_KEY"):
                self.skipped.append(mid)  # without Claude, newsletters are skipped; bookings, orders and job alerts are read
                continue
            ics = ""
            try:
                ics = event_line(self._ics(svc, mid, m["payload"]))
            except Exception:
                pass
            body = clean_body(_body(m["payload"]) or m.get("snippet", ""))
            cap = 20000 if is_job_alert(addr, name, h.get("subject", "")) else 2500 if bulk else 12000
            out.append(Message(
                source="gmail", msg_id=mid, thread_id=m["threadId"], sender=addr,
                sender_name=name or addr, is_from_me=(addr == me),
                text=((ics + "\n\n") if ics else "") + body[:cap],
                ts=datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc),
                subject=h.get("subject", "")))
        return out

    def _ics(self, svc, mid, payload):
        """Text of a calendar (.ics) attachment, if the email has one."""
        stack = [payload]
        while stack:
            p = stack.pop()
            stack += p.get("parts", []) or []
            if p.get("mimeType") in ("text/calendar", "application/ics") or (p.get("filename") or "").lower().endswith(".ics"):
                b = p.get("body", {})
                data = b.get("data")
                if not data and b.get("attachmentId"):
                    data = svc.users().messages().attachments().get(userId="me", messageId=mid, id=b["attachmentId"]).execute(
                        num_retries=1).get("data")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        return ""

    def events(self, start: datetime, end: datetime) -> list[dict]:
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=self._creds(), cache_discovery=False)
        r = svc.events().list(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                              singleEvents=True, orderBy="startTime", maxResults=20).execute()
        out = []
        for e in r.get("items", []):
            s, en = e.get("start", {}), e.get("end", {})
            if "dateTime" not in s:
                continue  # all-day events don't block time
            out.append({"title": e.get("summary") or "Busy", "start": s["dateTime"], "end": en.get("dateTime"),
                        "source": "google"})
        return out
