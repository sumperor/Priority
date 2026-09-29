"""Job leads: job-alert and recruiter emails, checked for credibility and fit before you see them.

Rules decide credibility and a first fit score with no API key. With a key, Claude reads the
job and the company (with web search) for up to MAX_AI_LEADS new leads per sync.
Only leads that look real and fit you are shown, as "worth applying" suggestions.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from .store import now_iso

MAX_AI_LEADS = 3
SHOW_FIT = 60

JOB_BOARDS = ("linkedin", "indeed", "glassdoor", "totaljobs", "reed.co.uk", "cv-library", "prospects.ac.uk",
              "gradcracker", "brightnetwork", "targetjobs", "milkround", "otta", "welcometothejungle", "wellfound",
              "joinhandshake", "ziprecruiter", "monster", "jobs.ac.uk", "guardianjobs", "civilservicejobs",
              "ratemyplacement", "trackr", "efinancialcareers")
ALERT_SUBJ = re.compile(r"(job alert|jobs? (for you|matching|you might)|new jobs?\b|recommended (jobs?|roles?)|"
                        r"is hiring|are hiring|roles? (for you|matching)|opportunit(y|ies) (for you|matching)|"
                        r"\d+ new (jobs?|roles?|opportunities))", re.I)
RECRUITER = re.compile(r"(came across your (profile|cv)|your (profile|background|experience) (looks|seems|caught|stood)|"
                       r"reach(ing)? out (about|regarding|with) (a|an|the) .{0,40}(role|position|opportunity)|"
                       r"(role|position|opportunity) (that )?(might|may|could|would) (interest|suit|be a (good|great) fit))", re.I)
URL = re.compile(r"https?://[^\s<>\"')\]]+")
JOB_URL = re.compile(r"(linkedin\.com/(comm/)?jobs/view|indeed\.[a-z.]+/(rc/clk|viewjob|pagead|m/viewjob)|"
                     r"glassdoor\.[a-z.]+/(job-listing|partner|Job)|greenhouse\.io|lever\.co|myworkdayjobs|"
                     r"smartrecruiters|workable\.com|ashbyhq|totaljobs\.com/job|reed\.co\.uk/jobs|otta\.com|"
                     r"welcometothejungle|gradcracker\.com/.+/job|brightnetwork\.co\.uk/graduate-jobs|"
                     r"targetjobs\.co\.uk/.+jobs|jobs\.ac\.uk/job|/careers?/|/jobs?/)", re.I)
SHORTENERS = ("bit.ly", "tinyurl", "t.co/", "goo.gl", "ow.ly", "rebrand.ly", "cutt.ly", "is.gd")
FREEMAIL = ("gmail.com", "yahoo.", "hotmail.", "outlook.com", "live.com", "aol.com", "icloud.com", "proton")
SKIP_LINE = re.compile(r"^(view (job|all|more)|apply( now)?|see (all|more)|unsubscribe|manage|job alert|"
                       r"new jobs?|save|share|easy apply|promoted|actively recruiting|\W*)$", re.I)
_WORDS = re.compile(r"[a-z][a-z+#.]{1,}")
_COMMON = {"and", "the", "for", "with", "role", "roles", "job", "jobs", "team", "senior", "junior", "graduate", "in",
           "of", "to", "a", "an", "at", "uk", "london", "remote", "hybrid", "scheme", "programme", "program", "entry",
           "level", "associate", "intern", "internship", "full", "time", "part", "ltd", "plc", "new"}


def _domain(addr):
    return addr.split("@")[-1].lower() if "@" in (addr or "") else ""


def is_job_alert(sender, sender_name="", subject=""):
    hay = f"{sender} {sender_name}".lower()
    return any(b in hay for b in JOB_BOARDS) or bool(ALERT_SUBJ.search(subject or ""))


def is_recruiter(m):
    return not m.is_from_me and bool(RECRUITER.search(f"{m.subject}\n{m.text}"))


def parse_alert(m, limit=10):
    """Pull (title, company, location, url) out of a job-alert email's plain text."""
    lines = [l.strip() for l in (m.text or "").splitlines()]
    out, seen = [], set()
    for i, line in enumerate(lines):
        for u in URL.findall(line):
            u = u.rstrip(".,;")
            key = u.split("?")[0].rstrip("/")
            if not JOB_URL.search(u) or key in seen or any(s in u for s in ("unsubscribe", "settings", "alerts")):
                continue
            seen.add(key)
            ctx, before = [], URL.sub("", line).strip(" :-|")
            if before and not SKIP_LINE.match(before):
                ctx.append(before)
            j = i - 1
            while j >= 0 and len(ctx) < 4 and i - j <= 6:
                t = lines[j]
                if t and not URL.search(t) and not SKIP_LINE.match(t):
                    ctx.insert(0, t)
                elif URL.search(t) and ctx:
                    break
                j -= 1
            ctx = [c for c in ctx if 1 < len(c) < 120][-3:]
            if not ctx:
                continue
            out.append({"title": ctx[0], "company": ctx[1] if len(ctx) > 1 else "", "location": ctx[2] if len(ctx) > 2 else "",
                        "url": u, "snippet": " · ".join(ctx)})
            if len(out) >= limit:
                return out
    return out


def parse_recruiter(m):
    text = f"{m.subject}\n{m.text}"
    role = re.search(r"\b(?:for|as) (?:a|an|the|our) ((?:[A-Z][\w/&+-]*\s?){1,6})(?:role|position)?", text)
    title = (role.group(1).strip() if role else (m.subject or "A role from a recruiter"))[:90]
    urls = [u for u in URL.findall(m.text or "") if JOB_URL.search(u)]
    company = m.sender_name.split("|")[-1].strip() if "|" in (m.sender_name or "") else ""
    return [{"title": title, "company": company, "location": "", "url": urls[0] if urls else "",
             "snippet": re.sub(r"\s+", " ", (m.text or "")[:400]).strip()}]


def credibility(lead, m, recruiter=False):
    """Plain rules, so there's always an answer. Flags are shown to you as they are."""
    text = f"{m.subject}\n{m.text}".lower()
    flags = []
    if re.search(r"\b(registration|training|processing|admin) fee|pay (a |an )?(fee|deposit|upfront)|bank details|"
                 r"wire transfer|gift cards?|bitcoin|crypto(currency)? (payment|wallet)|buy (your own )?equipment", text):
        flags.append("Asks for money or bank details")
    if re.search(r"(whatsapp|telegram|signal)\b.{0,30}\b(me|us|chat|contact|message)|(contact|message|text) (me|us) on (whatsapp|telegram)", text):
        flags.append("Wants to move the chat to WhatsApp or Telegram")
    if recruiter and any(f in _domain(m.sender) for f in FREEMAIL):
        flags.append("Recruiter writing from a personal email address")
    if re.search(r"(earn|make|paid) (up to )?[£$€]\s?\d[\d,]*k? (a|per) (day|hour|week)", text) and \
            re.search(r"no (experience|interview|qualifications?) (needed|required)", text):
        flags.append("Pay sounds too good for no experience")
    url = (lead.get("url") or "").lower()
    if any(s in url for s in SHORTENERS):
        flags.append("The link hides where it really goes")
    if flags:
        return "suspicious", flags
    known = bool(url) and bool(JOB_URL.search(url)) and any(b in url for b in JOB_BOARDS + ("greenhouse", "lever", "workday",
                                                                                          "smartrecruiters", "workable", "ashby"))
    board_sender = any(b in _domain(m.sender) for b in JOB_BOARDS)
    company_sender = lead.get("company") and re.sub(r"[^a-z]", "", lead["company"].lower())[:6] in _domain(m.sender)
    return ("credible" if known or board_sender or company_sender else "unverified"), []


def looks_like_scam(m):
    return is_recruiter(m) and credibility({"url": ""}, m, recruiter=True)[0] == "suspicious"


def _targets(store):
    """What you've said you're looking for, plus your CV."""
    roles, cv = [], ""
    try:
        for r in store.db.execute("SELECT answers FROM sessions ORDER BY id DESC LIMIT 5"):
            roles.append(json.loads(r["answers"] or "{}").get("roles", ""))
        for r in store.db.execute("SELECT role FROM applications ORDER BY id DESC LIMIT 20"):
            roles.append(r["role"] or "")
        from .jobs import profile
        cv = profile(store)[0]   # your CV, LinkedIn, or both, as you chose
    except Exception:
        pass  # session tables not created yet
    return " ".join(roles).lower(), cv.lower()


def rule_fit(lead, targets, cv):
    """0-100 from word overlap with the roles you've gone for and your CV. None if there's nothing to compare."""
    if not targets and not cv:
        return None, []
    words = {w for w in _WORDS.findall(lead["title"].lower()) if w not in _COMMON}
    if not words:
        return 50, []
    hit_t = sorted(w for w in words if w in targets)
    hit_cv = sorted(w for w in words if re.search(rf"\b{re.escape(w)}\b", cv))
    fit = 35 + 45 * len(hit_t) / len(words) + 25 * len(hit_cv) / len(words)
    reasons = []
    if hit_t:
        reasons.append(f"Matches roles you've gone for: {', '.join(hit_t[:4])}")
    if hit_cv:
        reasons.append(f"Your CV mentions {', '.join(hit_cv[:4])}")
    return int(min(95, round(fit))), reasons


def ai_review(lead, cv):
    from .llm import ask_chat
    jd = ""
    if lead.get("url"):
        try:
            from .session import _page_text
            jd = _page_text(lead["url"])[:8000]
        except Exception:
            jd = ""
    txt = ask_chat(
        "You screen job leads for a graduate job hunter. Be honest and specific, no em dashes. Reply ONLY with JSON.",
        [{"role": "user", "content":
          f"Lead: {lead['title']} at {lead['company'] or 'unknown company'} {lead['location']}\nLink: {lead['url']}\n"
          f"Email excerpt: {lead['snippet'][:600]}\nJob page text (may be empty or blocked): {jd}\n\nMy CV:\n{cv[:5000]}\n\n"
          'Check the company is real and the posting looks genuine, then judge fit. Return {"credible": "credible"|"unverified"|"suspicious", '
          '"flags": ["red flags, if any"], "fit": 0-100, "verdict": "one sentence: should I apply and why", '
          '"reasons": ["up to 3 specific reasons it fits or doesn\'t"]}'}], max_tokens=800, search=True)
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else None


def scan(store, since, use_ai=False):
    """Find new leads in messages since `since`. Returns how many were added."""
    from .models import Message  # noqa: F401
    targets, cv = _targets(store)
    ai_left, added = (MAX_AI_LEADS if use_ai else 0), 0
    for m in store.messages_since(since):
        key = f"leads:{m.source}:{m.msg_id}"
        if store.checked(key):
            continue
        alert, recruiter = is_job_alert(m.sender, m.sender_name, m.subject), is_recruiter(m)
        if not (alert or recruiter):
            continue
        store.mark_checked(key)
        for lead in (parse_alert(m) if alert else parse_recruiter(m)):
            ukey = (lead["url"] or f"{m.msg_id}:{lead['title']}").split("?")[0]
            if store.db.execute("SELECT 1 FROM leads WHERE url=?", (ukey,)).fetchone():
                continue
            cred, flags = credibility(lead, m, recruiter=recruiter and not alert)
            fit, reasons = rule_fit(lead, targets, cv)
            verdict = ""
            if ai_left and cred != "suspicious":
                ai_left -= 1
                try:
                    r = ai_review(lead, cv)
                    if r:
                        cred = r.get("credible") if r.get("credible") in ("credible", "unverified", "suspicious") else cred
                        flags = flags + [f for f in (r.get("flags") or []) if f][:3]
                        fit = int(max(0, min(100, r.get("fit", fit or 50))))
                        verdict = r.get("verdict") or ""
                        reasons = (r.get("reasons") or reasons)[:3]
                except Exception as e:
                    print(f"[loops] lead review fell back to rules: {e}")
            if not verdict:
                verdict = ("Looks like a real posting that fits what you're after." if cred == "credible" and (fit or 0) >= SHOW_FIT
                           else "Couldn't confirm this one is genuine." if cred != "credible" else "")
            store.db.execute(
                "INSERT OR IGNORE INTO leads(source,msg_id,title,company,location,url,snippet,credible,flags,fit,verdict,reasons,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m.source, m.msg_id, lead["title"], lead["company"], lead["location"], ukey, lead["snippet"], cred,
                 json.dumps(flags), fit, verdict, json.dumps(reasons), now_iso()))
            added += 1
        store.db.commit()
    return added


def shown(store, limit=5):
    """Leads worth your time: not suspicious, and a good fit (or fit unknown until you add a CV)."""
    rows = store.db.execute(
        "SELECT * FROM leads WHERE status='new' AND credible!='suspicious' AND (fit IS NULL OR fit>=?) "
        "ORDER BY (credible='credible') DESC, COALESCE(fit, 0) DESC, id DESC LIMIT ?", (SHOW_FIT, limit * 3)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"], d["reasons"] = json.loads(d["flags"] or "[]"), json.loads(d["reasons"] or "[]")
        d["review"] = json.loads(d["review"]) if d.get("review") else None
        d.pop("jd", None)
        if (d["review"] or {}).get("verdict") == "skip":
            continue
        out.append(d)
    return out[:limit]


def accept(store, lead_id):
    """'Apply' on a lead: it becomes a task in Jobs, and the inbox check will spot the confirmation email."""
    r = store.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        return None
    what = r["title"] + (f" at {r['company']}" if r["company"] else "")
    loop_id = store.create_loop(
        source="manual", thread_id="", type="task", person=r["company"] or "", area="Jobs",
        summary=f"Apply to {what}"[:120], done_when="An application confirmation email arrives",
        due=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(), cost=60, consequence="opportunity",
        reversible=1, hard_deadline=0, effort_h=1.0, stakes="Roles like this can close early.")
    store.db.execute("UPDATE leads SET status='applying', loop_id=? WHERE id=?", (loop_id, lead_id))
    store.db.commit()
    return loop_id
