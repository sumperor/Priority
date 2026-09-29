"""Job-hunt sessions: set objectives, work in focused blocks, get help on each application,
and keep an eye on everything around you (assessments, calendar, other deadlines)."""
import base64
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config as C
from .store import Store, now_iso

router = APIRouter(prefix="/api")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, answers TEXT, objectives TEXT, steps TEXT,
  target INTEGER, minutes INTEGER, focus_min INTEGER, break_min INTEGER, research INTEGER,
  started TEXT, ended TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, company TEXT, role TEXT, url TEXT, jd TEXT,
  research TEXT, status TEXT DEFAULT 'in_progress', created TEXT, submitted_at TEXT);
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS helper(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, app_id INTEGER, role TEXT, text TEXT, ts TEXT);
"""

COACH = ("You are a sharp, warm job-search coach working alongside a graduate job hunter in real time. "
         "Be concise and specific: short paragraphs or tight bullet points, no filler, no em dashes. "
         "Never invent facts about a company; say when you're unsure. Use their CV when tailoring advice.")


def db():
    s = Store(C.DB_PATH)
    s.db.executescript(SCHEMA)
    return s


def ai_on():
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def utcnow():
    return datetime.now(timezone.utc)


def active(s):
    return s.db.execute("SELECT * FROM sessions WHERE ended IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def cv_text(s):
    r = s.db.execute("SELECT text, name FROM docs WHERE kind='cv' ORDER BY id DESC LIMIT 1").fetchone()
    return (r["text"], r["name"]) if r else ("", "")


# ---------------------------------------------------------------- intake
INTAKE = [
    {"id": "kind", "q": "What do you want to get done in this session?", "type": "choice",
     "options": [{"label": "Apply to jobs", "value": "apply"}, {"label": "Complete an assessment", "value": "assessment"},
                 {"label": "Prepare for an interview", "value": "interview"},
                 {"label": "Learn a skill for my applications", "value": "upskill"},
                 {"label": "Something else", "value": "custom"}]},
    {"id": "target", "q": "How many applications are you aiming for?", "type": "choice", "when": {"kind": "apply"},
     "options": [{"label": "3", "value": "3"}, {"label": "5", "value": "5"}, {"label": "10", "value": "10"},
                 {"label": "As many as I can", "value": "0"}]},
    {"id": "roles", "q": "What kind of roles?", "type": "text", "when": {"kind": "apply"},
     "placeholder": "e.g. Graduate data analyst roles in London"},
    {"id": "which", "q": "Which assessment is it?", "type": "text", "when": {"kind": "assessment"},
     "placeholder": "e.g. Shell numerical reasoning test"},
    {"id": "which", "q": "Which interview, and when is it?", "type": "text", "when": {"kind": "interview"},
     "placeholder": "e.g. CIL first round, Thursday 10am"},
    {"id": "which", "q": "What do you want to learn, and what's it for?", "type": "text", "when": {"kind": "upskill"},
     "placeholder": "e.g. SQL window functions for data analyst tests"},
    {"id": "which", "q": "What do you want to get done?", "type": "text", "when": {"kind": "custom"},
     "placeholder": "e.g. Write the follow-up email to Priya"},
    {"id": "minutes", "q": "How long have you got?", "type": "choice",
     "options": [{"label": "30 minutes", "value": "30"}, {"label": "1 hour", "value": "60"},
                 {"label": "2 hours", "value": "120"}, {"label": "Until it's done", "value": "0"}]},
    {"id": "rhythm", "q": "How do you like to work?", "type": "choice",
     "options": [{"label": "25 min focus, 5 min break", "value": "25/5"}, {"label": "50 min focus, 10 min break", "value": "50/10"},
                 {"label": "No timer", "value": "0/0"}]},
    {"id": "research", "q": "Want me to research each role with you?", "type": "choice", "when": {"kind": "apply"},
     "options": [{"label": "Yes, research each one", "value": "1"}, {"label": "No, I'll just ask when I need to", "value": "0"}]},
]


@router.get("/session/intake")
def intake():
    return {"questions": INTAKE}


def _objectives_fallback(a):
    kind, n = a.get("kind", "apply"), int(a.get("target") or 0)
    roles = (a.get("roles") or "roles that fit you").strip().rstrip(".")
    if len(roles) > 1 and not roles[1].isupper():
        roles = roles[0].lower() + roles[1:]  # "Graduate data..." reads as a sentence: "Apply to 5 graduate data..."
    which = (a.get("which") or "").strip().rstrip(".")
    if kind == "apply":
        count = f"{n} applications" if n else "as many applications as you can, without rushing them"
        return {"title": f"Apply to {n or 'as many'} {roles}" if n else f"Apply to {roles}",
                "objectives": [f"Submit {count}", "Tailor your CV bullets to each role's top 3 requirements",
                               "Log each one here so follow-ups are tracked"],
                "steps": ["Paste the job link or description", "Check the match and gaps", "Tailor your CV and answers",
                          "Submit, then mark it submitted"]}
    if kind == "assessment":
        return {"title": f"Complete {which or 'the assessment'}",
                "objectives": ["Do one practice question to warm up", "Complete it in one sitting, somewhere quiet",
                               "Note what came up, for next time"], "steps": []}
    if kind == "custom":
        what = which[:1].upper() + which[1:] if which else "Get one thing done"
        return {"title": what,
                "objectives": ["Start with the smallest first step", "Finish it in this session",
                               "Tick it off on your list when it's done"], "steps": []}
    if kind == "interview":
        return {"title": f"Prepare for {which or 'the interview'}",
                "objectives": ["Research the company's recent news and deals", "Prepare 3 STAR stories matched to the role",
                               "Prepare 2 questions to ask them"], "steps": []}
    return {"title": f"Learn {which or 'a new skill'}",
            "objectives": ["Decide what 'learned it' means by the end", "Work through one resource in focused blocks",
                           "Write 3 takeaways you could mention in an interview"], "steps": []}


class Start(BaseModel):
    answers: dict


@router.post("/session/start")
def start(body: Start):
    s = db()
    if active(s):
        raise HTTPException(400, "A session is already running. End it first.")
    a = {k: str(v) for k, v in body.answers.items()}
    plan = _objectives_fallback(a)
    if ai_on():
        try:
            from .llm import ask_json
            cv, _ = cv_text(s)
            d = ask_json(COACH + " Reply ONLY with JSON.",
                         f"Session answers: {json.dumps(a)}\nCV (may be empty): {cv[:3000]}\n"
                         'Return {"title": "short session title", "objectives": ["3 to 4 concrete, checkable objectives"], '
                         '"steps": ["for job applications only: 3 to 5 steps to repeat per application, else empty"]}')
            if d.get("title") and d.get("objectives"):
                plan = {"title": d["title"], "objectives": d["objectives"][:4], "steps": (d.get("steps") or [])[:5]}
        except Exception as e:
            print(f"[loops] objectives fell back to template: {e}")
    focus, brk = (int(x) for x in a.get("rhythm", "25/5").split("/"))
    cur = s.db.execute(
        "INSERT INTO sessions(kind,title,answers,objectives,steps,target,minutes,focus_min,break_min,research,started) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (a.get("kind", "apply"), plan["title"], json.dumps(a), json.dumps(plan["objectives"]), json.dumps(plan["steps"]),
         int(a.get("target") or 0), int(a.get("minutes") or 0), focus, brk, int(a.get("research", "1")), now_iso()))
    s.db.commit()
    return {"id": cur.lastrowid}


# ---------------------------------------------------------------- the session view
def upcoming_events(hours=12):
    from .connectors import ALL
    now, out = utcnow(), []
    from .connect import connected
    for name in connected():
        c = ALL.get(name)
        if not c or not hasattr(c, "events"):
            continue
        try:
            out += c().events(now, now + timedelta(hours=hours))
        except Exception as e:
            print(f"[loops] calendar ({name}) unavailable: {e}")
    return sorted(out, key=lambda e: e["start"])


def _pace(sess, submitted, elapsed_min):
    if sess["kind"] != "apply":
        return ""
    target, minutes = sess["target"], sess["minutes"]
    if target and minutes:
        left_apps, left_min = max(0, target - submitted), max(0, minutes - elapsed_min)
        if not left_apps:
            return "Target hit. Anything more is a bonus."
        per = left_min / left_apps if left_apps else 0
        return f"{left_apps} to go in {left_min} min, about {round(per)} min each."
    if submitted and elapsed_min:
        return f"About {round(elapsed_min / submitted)} min per application so far."
    return ""


@router.get("/session")
def get_session():
    s = db()
    sess = active(s)
    cv, cv_name = cv_text(s)
    if not sess:
        last = s.db.execute("SELECT * FROM sessions WHERE ended IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return {"active": None, "cv": cv_name, "last": dict(last) if last else None, "ai": ai_on()}
    apps = [dict(r) for r in s.db.execute("SELECT * FROM applications WHERE session_id=? ORDER BY id", (sess["id"],))]
    for a in apps:
        a["research"] = json.loads(a["research"]) if a["research"] else None
        a.pop("jd", None)
    elapsed = round((utcnow() - datetime.fromisoformat(sess["started"])).total_seconds() / 60)
    submitted = sum(1 for a in apps if a["status"] == "submitted")
    assessments = [dict(r) for r in s.db.execute(
        "SELECT id, summary, due, person, effort_h FROM loops WHERE type='assessment' AND status!='closed' ORDER BY due")]
    from .engine import ranked
    other = [{"id": r["id"], "summary": r["summary"], "why": r.get("why", ""), "due": r["due"]}
             for r in ranked(s) if r["bucket"] == "Do now" and r["type"] != "assessment"][:3]
    msgs = [dict(r) for r in s.db.execute("SELECT * FROM helper WHERE session_id=? ORDER BY id", (sess["id"],))]
    out = dict(sess)
    for k in ("answers", "objectives", "steps"):
        out[k] = json.loads(out[k] or "[]")
    return {"active": out, "apps": apps, "elapsed_min": elapsed, "submitted": submitted,
            "pace": _pace(sess, submitted, elapsed), "assessments": assessments, "events": upcoming_events(),
            "other": other, "cv": cv_name, "helper": msgs, "ai": ai_on()}


@router.post("/session/end")
def end():
    s = db()
    sess = active(s)
    if not sess:
        raise HTTPException(400, "No session is running.")
    apps = s.db.execute("SELECT * FROM applications WHERE session_id=?", (sess["id"],)).fetchall()
    sub = [a for a in apps if a["status"] == "submitted"]
    mins = round((utcnow() - datetime.fromisoformat(sess["started"])).total_seconds() / 60)
    summary = f"{mins} min. " + (f"{len(sub)} application{'s' if len(sub) != 1 else ''} submitted." if sess["kind"] == "apply"
                                 else "Session complete.")
    s.db.execute("UPDATE sessions SET ended=?, summary=? WHERE id=?", (now_iso(), summary, sess["id"]))
    s.db.commit()
    return {"summary": summary, "submitted": [f"{a['role']} at {a['company']}" for a in sub]}


# ---------------------------------------------------------------- applications
def _page_text(url):
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Loops job helper"})
    r.raise_for_status()
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", r.text)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _guess(jd):
    lines = [l.strip() for l in jd.splitlines() if l.strip()]
    role = lines[0][:80] if lines else "Role"
    m = re.search(r"\b(?i:at|join)\s+([A-Z][\w&.\- ]{1,40}?)(?:[,.!]|\s+(?:is|as|we|and|in)\b)", jd)
    return (m.group(1).strip() if m else "Company"), role


class NewApp(BaseModel):
    input: str


@router.post("/apps")
def add_app(body: NewApp):
    s = db()
    sess = active(s)
    if not sess:
        raise HTTPException(400, "Start a session first.")
    raw = body.input.strip()
    url, jd = "", raw
    if re.match(r"^https?://\S+$", raw):
        url = raw
        try:
            jd = _page_text(url)
        except Exception:
            jd = ""
        if len(jd) < 400:
            raise HTTPException(400, "Couldn't read that page (many job sites block this). Paste the job description text instead.")
    jd = jd[:15000]
    company, role = _guess(jd)
    research = None
    if ai_on() and sess["research"]:
        try:
            from .llm import ask_chat
            cv, _ = cv_text(s)
            txt = ask_chat(COACH + " Reply ONLY with JSON, no prose around it.", [{"role": "user", "content":
                f"Job description:\n{jd}\n\nMy CV (may be empty):\n{cv[:6000]}\n\n"
                'Research this role and return {"company": "", "role": "", "deadline": "date or null", '
                '"summary": "2 sentences on the role", "top_requirements": ["3 to 5"], '
                '"match": ["where my CV clearly fits, max 3"], "gaps": ["honest gaps, max 3"], '
                '"talking_points": ["specific points for why this company, max 3, from real recent facts if you found any"], '
                '"watch_out": "one thing to be careful about, or null"}'}], max_tokens=1400, search=True)
            m = re.search(r"\{.*\}", txt, re.S)
            research = json.loads(m.group(0)) if m else None
            if research:
                company = research.get("company") or company
                role = research.get("role") or role
        except Exception as e:
            print(f"[loops] research failed: {e}")
    cur = s.db.execute("INSERT INTO applications(session_id,company,role,url,jd,research,created) VALUES(?,?,?,?,?,?,?)",
                       (sess["id"], company, role, url, jd, json.dumps(research) if research else None, now_iso()))
    s.db.commit()
    return {"id": cur.lastrowid, "company": company, "role": role, "researched": bool(research)}


class Status(BaseModel):
    status: str  # submitted | skipped | in_progress
    company: str | None = None
    role: str | None = None


@router.post("/apps/{app_id}")
def app_status(app_id: int, body: Status):
    s = db()
    a = s.db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not a:
        raise HTTPException(404, "No such application")
    company, role = body.company or a["company"], body.role or a["role"]
    s.db.execute("UPDATE applications SET status=?, company=?, role=?, submitted_at=? WHERE id=?",
                 (body.status, company, role, now_iso() if body.status == "submitted" else None, app_id))
    s.db.commit()
    if body.status == "submitted" and a["status"] != "submitted":
        # Every submitted application becomes something to hear back about
        s.create_loop(source="manual", thread_id="", type="waiting", person=company,
                      summary=f"Hear back from {company} about {role}", done_when="A reply from the employer",
                      due=(utcnow() + timedelta(days=10)).isoformat(), cost=40, consequence="opportunity",
                      reversible=1, hard_deadline=0, effort_h=0.1,
                      stakes="Silence can mean it's stalled; a polite nudge after 10 days is normal.")
    return {"ok": True}


# ---------------------------------------------------------------- CV upload
class Doc(BaseModel):
    name: str
    b64: str | None = None
    text: str | None = None


@router.post("/docs/cv")
def upload_cv(body: Doc):
    text = body.text or ""
    if body.b64:
        raw = base64.b64decode(body.b64.split(",")[-1])
        if body.name.lower().endswith(".pdf"):
            try:
                from pypdf import PdfReader
                text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
            except ImportError:
                raise HTTPException(400, "Reading PDFs needs one more package. In Terminal run: pip install pypdf")
        else:
            text = raw.decode("utf-8", "ignore")
    if len(text.strip()) < 100:
        raise HTTPException(400, "Couldn't read text from that file. Try a PDF exported from Word, or a .txt file.")
    s = db()
    s.db.execute("INSERT INTO docs(kind,name,text,created) VALUES('cv',?,?,?)", (body.name, text[:30000], now_iso()))
    s.db.commit()
    return {"ok": True, "chars": len(text)}


# ---------------------------------------------------------------- helper chat
class Ask(BaseModel):
    question: str
    app_id: int | None = None


@router.post("/helper")
def helper(body: Ask):
    s = db()
    sess = active(s)
    if not sess:
        raise HTTPException(400, "Start a session first.")
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "Ask something first.")
    s.db.execute("INSERT INTO helper(session_id,app_id,role,text,ts) VALUES(?,?,?,?,?)", (sess["id"], body.app_id, "you", q, now_iso()))
    if not ai_on():
        ans = "The helper needs your Anthropic API key in .env. Everything else in the session works without it."
    else:
        cv, _ = cv_text(s)
        ctx = f"Session goal: {sess['title']}\nMy CV:\n{cv[:6000] or '(not uploaded)'}\n"
        if body.app_id:
            a = s.db.execute("SELECT * FROM applications WHERE id=?", (body.app_id,)).fetchone()
            if a:
                ctx += f"\nCurrent application: {a['role']} at {a['company']}\nJob description:\n{(a['jd'] or '')[:6000]}\n"
                if a["research"]:
                    ctx += f"Research so far: {a['research']}\n"
        hist = s.db.execute("SELECT role, text FROM helper WHERE session_id=? ORDER BY id DESC LIMIT 9", (sess["id"],)).fetchall()[::-1]
        msgs = [{"role": "user" if h["role"] == "you" else "assistant", "content": h["text"]} for h in hist]
        msgs[0] = {"role": "user", "content": ctx + "\n" + msgs[0]["content"]} if msgs[0]["role"] == "user" else msgs[0]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        try:
            from .llm import ask_chat
            ans = ask_chat(COACH, msgs, max_tokens=900, search=True) or "I couldn't come up with an answer. Try rephrasing."
        except Exception as e:
            ans = f"Couldn't reach Claude: {e}"
    s.db.execute("INSERT INTO helper(session_id,app_id,role,text,ts) VALUES(?,?,?,?,?)", (sess["id"], body.app_id, "coach", ans, now_iso()))
    s.db.commit()
    return {"answer": ans}


# ---------------------------------------------------------------- check-ins between focus blocks
class Checkin(BaseModel):
    mood: str                 # good | stuck | tired
    focus_minutes: int = 0    # focused minutes so far this session


def _wellbeing(mins):
    if mins >= 120:
        return f"You've focused for {mins // 60}h {mins % 60} min. Eat something before the next block."
    if mins >= 60:
        return f"You've been at it for {mins} min. Get some water and stand up for a minute."
    return ""


@router.post("/session/checkin")
def checkin(body: Checkin):
    s = db()
    sess = active(s)
    if not sess:
        raise HTTPException(400, "No session is running.")
    now, lines, offer, brk = utcnow(), [], None, sess["break_min"] or 5
    if body.mood == "tired":
        brk = max(15, brk)
        lines.append(f"Take a proper break: {brk} minutes away from the screen.")
    elif body.mood == "stuck":
        lines.append("What's blocking you? Ask the helper, or skip this one and come back to it.")
    else:
        a = s.db.execute("SELECT id, summary, due, effort_h FROM loops WHERE type='assessment' AND status!='closed' "
                         "ORDER BY due LIMIT 1").fetchone()
        if a:
            due = datetime.fromisoformat(a["due"]).astimezone()
            offer = {"id": a["id"], "summary": a["summary"]}
            lines.append(f"Feeling up for it? {a['summary']} takes about {round((a['effort_h'] or 1) * 60)} min "
                         f"and is due {due:%A %H:%M}. Do it in the next block?")
        else:
            elapsed = round((now - datetime.fromisoformat(sess["started"])).total_seconds() / 60)
            sub = s.db.execute("SELECT COUNT(*) n FROM applications WHERE session_id=? AND status='submitted'",
                               (sess["id"],)).fetchone()["n"]
            lines.append("Good. Keep going. " + _pace(sess, sub, elapsed))
    w = _wellbeing(body.focus_minutes)
    if w:
        lines.append(w)
    for e in upcoming_events(hours=2):
        start = datetime.fromisoformat(e["start"])
        mins = (start - now).total_seconds() / 60
        if 0 < mins <= 45:
            lines.append(f"You have \u201c{e['title']}\u201d at {start.astimezone():%H:%M}. Wrap up by "
                         f"{(start - timedelta(minutes=10)).astimezone():%H:%M}, or skip it if it can wait.")
            break
    return {"message": " ".join(l for l in lines if l).strip(), "offer": offer, "break_min": brk}
