"""Jobs from your inbox: find them, judge them against your profile, research the firm, tailor your CV.

Your profile is your CV, your LinkedIn profile, or both. Claude does the reading and research when
an API key is set; without one, plain rules give a first verdict, the application steps they can spot
in the job text, and your CV with the job's key words to work in.
"""
import json
import re
import threading
from datetime import datetime, timedelta, timezone

from . import config as C
from .store import now_iso

scan_state = {"running": False, "found": 0, "checked": 0, "error": "", "done_at": None, "emails": 0}
check_state = {"running": False, "done": 0, "total": 0, "error": ""}

PROCESS = [
    (r"\b(online (test|assessment)|psychometric|numerical|verbal|logical reasoning|situational judg\w+|game[- ]based)\b", "Online tests"),
    (r"\b(video interview|hirevue|recorded interview|one[- ]way interview)\b", "Video interview"),
    (r"\b(phone|telephone) (interview|screen)\b", "Phone interview"),
    (r"\b(assessment cent(re|er)|final round|superday)\b", "Assessment centre or final round"),
    (r"\b(case study|technical test|take[- ]home|coding (test|challenge))\b", "Case study or technical test"),
]


# ---------------------------------------------------------------- your profile
def prefs():
    d = C.read_secret("profile.json")
    d.setdefault("compare_with", "cv")
    return d


def set_interests(text):
    d = prefs()
    d["interests"] = (text or "").strip()[:1500]
    C.write_secret("profile.json", d)
    return d


def set_compare(value):
    d = prefs()
    d["compare_with"] = value if value in ("cv", "linkedin", "both") else "cv"
    C.write_secret("profile.json", d)
    return d


def _doc(store, kind):
    try:
        r = store.db.execute("SELECT name, text FROM docs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return (r["name"], r["text"]) if r else ("", "")
    except Exception:
        return "", ""


def profile(store):
    """(text, label): what you chose to be compared with, plus what you said you're looking for."""
    text, label = _profile_docs(store)
    want = prefs().get("interests", "")
    if want:
        text = f"{text}\n\nWHAT I'M LOOKING FOR:\n{want}".strip()
        label = f"{label} and what you're looking for" if label else "what you're looking for"
    return text, label


def _profile_docs(store):
    cmp = prefs()["compare_with"]
    cv, li = _doc(store, "cv"), _doc(store, "linkedin")
    if cmp == "linkedin" and li[1]:
        return li[1], "your LinkedIn profile"
    if cmp == "both" and (cv[1] or li[1]):
        return f"CV:\n{cv[1]}\n\nLINKEDIN:\n{li[1]}".strip(), "your CV and LinkedIn"
    if cv[1]:
        return cv[1], f"your CV ({cv[0]})"
    if li[1]:
        return li[1], "your LinkedIn profile"
    return "", ""


def save_doc(store, kind, name, text):
    from .session import SCHEMA
    store.db.executescript(SCHEMA)
    store.db.execute("INSERT INTO docs(kind,name,text,created) VALUES(?,?,?,?)", (kind, name, text[:30000], now_iso()))
    store.db.commit()


def linkedin_from_url(store, url):
    """Read a public LinkedIn profile the way ChatGPT or Claude would: web search and the public page."""
    from .llm import ask_chat
    txt = ask_chat("You read a person's public professional profile for them, using web search. Only include what you "
                   "actually found on public pages. Never guess. Plain text, no em dashes.",
                   [{"role": "user", "content":
                     f"This is my LinkedIn profile: {url}\nSearch for it and write out everything public: headline, "
                     "current role, experience with dates, education, skills, certifications. If you can only see part of it, "
                     "start your answer with PARTIAL: and say what was missing. If you found nothing, reply only NOTHING."}],
                   max_tokens=1500, search=True)
    if not txt or txt.strip().upper().startswith("NOTHING") or len(txt) < 120:
        return None, "LinkedIn didn't show enough of your profile publicly. Upload the LinkedIn PDF instead."
    partial = txt.strip().upper().startswith("PARTIAL")
    save_doc(store, "linkedin", url, txt)
    return txt, ("Only part of your profile was public, so some sections may be missing. The LinkedIn PDF is more complete."
                 if partial else "")


# ---------------------------------------------------------------- scanning the inbox
def scan_inbox(store_factory, days=14):
    """Background job: fetch job emails from connected mail accounts, then find and check leads."""
    from .connect import connected
    from .connectors import ALL
    from .leads import scan
    if scan_state["running"]:
        return
    scan_state.update(running=True, found=0, checked=0, error="")
    try:
        store = store_factory()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        for name in connected():
            c = ALL[name]()
            if hasattr(c, "fetch_jobs"):
                try:
                    store.upsert_messages(c.fetch_jobs(since, known=store.known_ids(name)))
                except Exception as e:
                    scan_state["error"] = f"{name}: {e}"[:200]
        before = store.db.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        # re-read messages we'd already seen too, so old alerts in the window become leads
        for r in store.db.execute("SELECT source, msg_id FROM messages WHERE ts >= ?", (since.isoformat(),)).fetchall():
            store.db.execute("DELETE FROM checks WHERE key=?", (f"leads:{r['source']}:{r['msg_id']}",))
        store.db.commit()
        scan(store, since, use_ai=False)
        scan_state["found"] = store.db.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"] - before
        scan_state["emails"] = store.db.execute("SELECT COUNT(DISTINCT msg_id) n FROM leads").fetchone()["n"]
        # nothing is judged here: the jobs are listed, and relevance is checked when you ask
    except Exception as e:
        scan_state["error"] = str(e)[:200]
    finally:
        scan_state["running"] = False
        scan_state["done_at"] = now_iso()


def start_scan(store_factory, days=14):
    threading.Thread(target=scan_inbox, args=(store_factory, days), daemon=True).start()


def _ai():
    import os
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------- judging one job
def _jd(store, lead):
    if lead["jd"]:
        return lead["jd"]
    jd = ""
    if lead["url"] and lead["url"].startswith("http"):
        try:
            from .session import _page_text
            jd = _page_text(lead["url"])[:15000]
        except Exception:
            jd = ""
    if len(jd) < 300:
        jd = ""
    store.db.execute("UPDATE leads SET jd=? WHERE id=?", (jd or " ", lead["id"]))
    store.db.commit()
    return jd


def _format(text):
    t = (text or "").lower()
    if re.search(r"\b(pdf)\b", t) and not re.search(r"\b(word|docx?)\b", t):
        return "pdf"
    if re.search(r"\b(word document|\.docx?|ms word)\b", t) and "pdf" not in t:
        return "word"
    if re.search(r"application form|fill (in|out) (the|our) form|no cvs?\b", t):
        return "form"
    return "any"


def rule_review(lead, jd, profile_text):
    from .leads import rule_fit
    text = f"{lead['title']} {lead['snippet']} {jd}"
    fit, reasons = rule_fit(lead, "", profile_text.lower())
    steps = [label for pat, label in PROCESS if re.search(pat, text, re.I)]
    dl = re.search(r"(closing date|deadline|apply by|applications close)[:\s]+([^\n.;]{3,40})", text, re.I)
    verdict = "skip" if lead["credible"] == "suspicious" else "apply" if (fit or 0) >= 70 else "maybe" if (fit or 0) >= 45 else "skip"
    return {"verdict": verdict,
            "why": ("It matches words in your profile." if reasons else "Few of the job's words appear in your profile.")
                   if profile_text else "Add your CV or LinkedIn to get a real verdict.",
            "match": reasons, "gaps": [],
            "process": {"steps": steps or ["Not stated in what Sparrow could read"], "deadline": dl.group(2).strip() if dl else "",
                        "cv_format": _format(jd), "cover_letter": bool(re.search(r"cover(ing)? letter", text, re.I))},
            "firm": {}, "watch_out": "", "sources": [], "checked_by": "rules", "read_job_page": bool(jd)}


def review(store, lead_id, use_ai=None):
    use_ai = _ai() if use_ai is None else use_ai
    lead = store.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return None
    lead = dict(lead)
    jd = _jd(store, lead).strip()
    ptext, plabel = profile(store)
    out = rule_review(lead, jd, ptext)
    if use_ai:
        try:
            from .llm import ask_chat
            txt = ask_chat(
                "You are a sharp, honest careers adviser for a graduate job hunter. Use web search to research the firm and, "
                "if the job text is missing, the role. Never invent facts about the person; judge only from their profile. "
                "Plain words, no em dashes. Reply ONLY with JSON.",
                [{"role": "user", "content":
                  f"JOB: {lead['title']} at {lead['company'] or 'unknown'} ({lead['location']})\nLink: {lead['url']}\n"
                  f"From the email: {lead['snippet'][:500]}\nJob page text (may be empty): {jd[:9000]}\n\n"
                  f"MY PROFILE ({plabel or 'none given'}):\n{ptext[:7000]}\n\n"
                  'Return {"verdict": "apply"|"maybe"|"skip", "why": "two sentences, specific", '
                  '"match": ["up to 4 specific ways my profile fits"], "gaps": ["up to 3 honest gaps and how to handle each"], '
                  '"process": {"steps": ["the application stages in order"], "deadline": "date or empty", '
                  '"cv_format": "pdf"|"word"|"form"|"any", "cover_letter": true|false}, '
                  '"firm": {"what": "one sentence on what they do", "recent": ["up to 3 recent facts or news"], '
                  '"why_them": ["up to 3 points for why this firm, tied to my profile"], "culture": "one sentence or empty"}, '
                  '"watch_out": "one thing to be careful about, or empty", "sources": ["urls you used"]}'}],
                max_tokens=1800, search=True)
            m = re.search(r"\{.*\}", txt, re.S)
            d = json.loads(m.group(0)) if m else None
            if d and d.get("verdict") in ("apply", "maybe", "skip"):
                proc = {**out["process"], **{k: v for k, v in (d.get("process") or {}).items() if v not in (None, "", [])}}
                out.update({"verdict": d["verdict"], "why": d.get("why") or out["why"], "match": (d.get("match") or [])[:4],
                            "gaps": (d.get("gaps") or [])[:3], "process": proc, "firm": d.get("firm") or {},
                            "watch_out": d.get("watch_out") or "", "checked_by": "claude",
                            "sources": [u for u in (d.get("sources") or []) if str(u).startswith("http")][:6]})
        except Exception as e:
            print(f"[sparrow] job review fell back to rules: {e}")
    out["compared_with"] = plabel
    store.db.execute("UPDATE leads SET review=? WHERE id=?", (json.dumps(out), lead_id))
    store.db.commit()
    return out


def check_all(store_factory):
    """Background: check the relevance of every job not checked yet, one at a time."""
    if check_state["running"]:
        return
    store = store_factory()
    ids = [r["id"] for r in store.db.execute("SELECT id FROM leads WHERE status NOT IN ('skipped') AND review IS NULL "
                                             "AND credible!='suspicious' ORDER BY id DESC LIMIT 60")]
    check_state.update(running=True, done=0, total=len(ids), error="")

    def run():
        try:
            st = store_factory()
            for i in ids:
                try:
                    review(st, i)
                except Exception as e:
                    check_state["error"] = str(e)[:200]
                check_state["done"] += 1
        finally:
            check_state["running"] = False
    import threading
    threading.Thread(target=run, daemon=True).start()


def all_jobs(store):
    rows = store.db.execute("SELECT * FROM leads WHERE status NOT IN ('skipped') ORDER BY id DESC LIMIT 200").fetchall()
    order = {"apply": 0, "maybe": 1, None: 2, "skip": 3}
    out = []
    for r in rows:
        d = dict(r)
        d.pop("jd", None)
        d["flags"], d["reasons"] = json.loads(d["flags"] or "[]"), json.loads(d["reasons"] or "[]")
        d["review"] = json.loads(d["review"]) if d.get("review") else None
        out.append(d)
    return sorted(out, key=lambda d: (d["credible"] == "suspicious", order.get((d["review"] or {}).get("verdict")),
                                      -(d["fit"] or 0)))


# ---------------------------------------------------------------- a CV for this job
def _sections(text):
    """Split a plain-text CV into (heading, lines)."""
    out, cur = [], ("", [])
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        head = (line.isupper() and len(line) < 40) or re.fullmatch(
            r"(education|experience|work experience|professional experience|skills|projects|leadership|interests|"
            r"certifications|achievements|volunteering|languages|profile|summary|about)[:\s]*", line, re.I)
        if head:
            if cur[1] or cur[0]:
                out.append(cur)
            cur = (line.strip(": ").title(), [])
        else:
            cur[1].append(line)
    if cur[1] or cur[0]:
        out.append(cur)
    return out


def rule_cv(lead, jd, ptext):
    words = {w for w in re.findall(r"[a-z][a-z+#.]{2,}", f"{lead['title']} {jd}".lower())}
    from .leads import _COMMON
    have = {w for w in words if re.search(rf"\b{re.escape(w)}\b", ptext.lower())}
    common = [w for w, _ in sorted(((w, jd.lower().count(w)) for w in words - _COMMON), key=lambda x: -x[1])][:25]
    missing = [w for w in common if w not in have][:8]
    lines = [l for l in ptext.strip().splitlines() if l.strip()]
    secs = _sections("\n".join(lines[2:]))   # first two lines are your name and contact details
    name = lines[0].strip() if lines else ""
    return {"name": name, "contact": lines[1].strip() if len(lines) > 1 else "", "headline": lead["title"], "summary": "",
            "sections": [{"title": h or "Profile", "entries": [{"title": "", "org": "", "dates": "", "bullets": l}]} for h, l in secs][:8],
            "skills": [], "made_by": "rules",
            "notes": (["Without your API key this is your CV as it is. Work these words from the job in, where they're true for you: "
                       + ", ".join(missing)] if missing else ["Your CV already uses the job's main words."])}


def tailor_cv(store, lead_id, use_ai=None):
    use_ai = _ai() if use_ai is None else use_ai
    lead = dict(store.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    jd = _jd(store, lead).strip()
    ptext, plabel = profile(store)
    if not ptext:
        raise ValueError("Add your CV or LinkedIn first, so there's something to tailor.")
    rv = json.loads(lead["review"]) if lead.get("review") else {}
    cv = rule_cv(lead, jd, ptext)
    if use_ai:
        try:
            from .llm import ask_json
            d = ask_json(
                "You tailor a graduate's CV to one job. Use ONLY facts in their profile: never invent employers, dates, grades, "
                "numbers or skills. Reorder, cut and reword so the most relevant evidence comes first, using the job's language "
                "where it's true. One page: about 450 words. UK English, plain words, no em dashes. Reply ONLY with JSON.",
                f"JOB: {lead['title']} at {lead['company']}\nJob text: {jd[:7000] or lead['snippet']}\n"
                f"What matters for this job: {json.dumps(rv.get('match', []) + rv.get('gaps', []))}\n\nPROFILE ({plabel}):\n{ptext[:9000]}\n\n"
                'Return {"name": "", "contact": "email, phone, city, links from the profile", "headline": "one line aimed at this role", '
                '"summary": "2 sentences", "sections": [{"title": "Experience|Education|Projects|Leadership|...", '
                '"entries": [{"title": "role or degree", "org": "", "dates": "", "bullets": ["strong, specific bullets"]}]}], '
                '"skills": ["most relevant first"], "notes": ["what you changed and why, and anything true they should add"]}',
                max_tokens=2200)
            if d.get("sections"):
                cv = {**d, "made_by": "claude"}
        except Exception as e:
            print(f"[sparrow] CV tailoring fell back to rules: {e}")
    cv["for"] = f"{lead['title']}" + (f" at {lead['company']}" if lead["company"] else "")
    cv["format"] = (rv.get("process") or {}).get("cv_format") or _format(jd)
    cur = store.db.execute("INSERT INTO summaries(loop_id,kind,body,created) VALUES(?,?,?,?)",
                           (lead_id, "cv", json.dumps(cv), now_iso()))
    store.db.commit()
    cv["id"] = cur.lastrowid
    cv["text"] = cv_text(cv)
    return cv


def cv_text(cv):
    out = [cv.get("name", ""), cv.get("contact", ""), ""]
    if cv.get("headline"):
        out += [cv["headline"], ""]
    if cv.get("summary"):
        out += [cv["summary"], ""]
    for s in cv.get("sections", []):
        out.append(s["title"].upper())
        for e in s.get("entries", []):
            head = " | ".join(x for x in (e.get("title"), e.get("org"), e.get("dates")) if x)
            if head:
                out.append(head)
            out += [f"- {b}" if cv.get("made_by") == "claude" else b for b in e.get("bullets", [])]
        out.append("")
    if cv.get("skills"):
        out += ["SKILLS", ", ".join(cv["skills"])]
    return "\n".join(x for x in out if x is not None).strip()


def cv_html(cv):
    import html
    e = html.escape
    parts = [f"<h1>{e(cv.get('name', ''))}</h1><p class='c'>{e(cv.get('contact', ''))}</p>"]
    if cv.get("headline"):
        parts.append(f"<p class='hl'>{e(cv['headline'])}</p>")
    if cv.get("summary"):
        parts.append(f"<p>{e(cv['summary'])}</p>")
    for s in cv.get("sections", []):
        parts.append(f"<h2>{e(s['title'])}</h2>")
        for en in s.get("entries", []):
            if en.get("title") or en.get("org"):
                parts.append(f"<div class='row'><b>{e(en.get('title', ''))}</b>"
                             f"<span>{e(en.get('org', ''))}</span><i>{e(en.get('dates', ''))}</i></div>")
            parts.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in en.get("bullets", [])) + "</ul>")
    if cv.get("skills"):
        parts.append(f"<h2>Skills</h2><p>{e(', '.join(cv['skills']))}</p>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>CV</title><style>"
            "@page{size:A4;margin:14mm 15mm}body{font:10.5pt/1.38 'Helvetica Neue',Arial,sans-serif;color:#111;max-width:180mm;margin:24px auto}"
            "h1{font-size:20pt;margin:0}p.c{margin:2px 0 8px;color:#444}p.hl{font-weight:600;margin:0 0 6px}"
            "h2{font-size:10.5pt;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #999;padding-bottom:2px;margin:12px 0 5px}"
            ".row{display:flex;gap:8px;align-items:baseline}.row span{color:#333}.row i{margin-left:auto;font-style:normal;color:#555}"
            "ul{margin:2px 0 6px;padding-left:16px}li{margin:1px 0}"
            ".bar{font:14px sans-serif;background:#eef;padding:10px;border-radius:8px;margin-bottom:16px}@media print{.bar{display:none}}"
            "</style></head><body><div class='bar'>Use your browser's Print, then Save as PDF. This bar won't be printed.</div>"
            + "".join(parts) + "<script>if(location.search.includes('print'))setTimeout(()=>print(),400)</script></body></html>")


def cv_docx(cv):
    """A simple Word file, built by hand so there's nothing extra to install."""
    import io
    import zipfile
    from xml.sax.saxutils import escape as x

    def para(text, bold=False, size=21, space=60, caps=False, color=None):
        rpr = ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") + f'<w:sz w:val="{size}"/>' + ("<w:caps/>" if caps else "")
        return (f'<w:p><w:pPr><w:spacing w:after="{space}"/></w:pPr><w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>{rpr}</w:rPr>'
                f'<w:t xml:space="preserve">{x(text)}</w:t></w:r></w:p>')

    body = [para(cv.get("name", ""), bold=True, size=36, space=20), para(cv.get("contact", ""), size=19, color="444444", space=120)]
    if cv.get("headline"):
        body.append(para(cv["headline"], bold=True))
    if cv.get("summary"):
        body.append(para(cv["summary"], space=120))
    for s in cv.get("sections", []):
        body.append(para(s["title"], bold=True, caps=True, size=21, space=40))
        for en in s.get("entries", []):
            head = " | ".join(v for v in (en.get("title"), en.get("org"), en.get("dates")) if v)
            if head:
                body.append(para(head, bold=True, space=20))
            body += [para(f"• {b}", space=20) for b in en.get("bullets", [])]
        body.append(para("", space=40))
    if cv.get("skills"):
        body += [para("Skills", bold=True, caps=True, space=40), para(", ".join(cv["skills"]))]
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>' + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="850" w:right="900" w:bottom="850" w:left="900"/></w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
    return buf.getvalue()
