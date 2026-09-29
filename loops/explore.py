"""Explore: who is this person, and what might the meeting be about?

With an API key, Claude searches the web for their public professional profile and reads the
messages you already have with them. Without a key, it works out their organisation from the
email address and gives you ready-made searches. Only public, professional information is used.
"""
import json
import re
from urllib.parse import quote_plus

from .store import now_iso

FREEMAIL = ("gmail.", "googlemail.", "yahoo.", "hotmail.", "outlook.", "live.", "icloud.", "aol.", "proton", "me.com")
SYSTEM = ("You help a graduate job hunter prepare to meet someone. Research ONLY public, professional information "
          "(current role, employer, career history, public work, talks, posts). Never include home addresses, family, "
          "health, or other private details. If several people share the name, say so and use the email domain and "
          "the messages to pick the likely one, or say you couldn't tell. Never invent facts. Plain words, no em dashes. "
          "Reply ONLY with JSON.")
REASONS = [
    (r"\b(interview|assessment|hiring|recruit\w*|talent|role|position|vacanc)", "It's likely about a role: an interview, a screening call, or next steps in an application."),
    (r"\b(coffee|chat|catch up|connect|network|introduc)", "It looks like an informal chat or introduction, probably networking."),
    (r"\b(project|client|proposal|deck|pitch|demo|partnership)", "It may be about a project or piece of work."),
    (r"\b(supervis|dissertation|thesis|module|lecture|course|tutor)", "It may be about your studies."),
]


def _org_from(address):
    dom = address.split("@")[-1].lower() if "@" in (address or "") else ""
    if not dom or any(f in dom for f in FREEMAIL):
        return ""
    core = re.sub(r"^(mail|email|careers|jobs|hr|people)\.", "", dom).split(".")[0]
    if len(core) <= 2:
        return ""
    return core.upper() if len(core) <= 4 else core.replace("-", " ").title()   # kpmg -> KPMG, deloitte -> Deloitte


def _thread(store, loop):
    if loop.get("source") in (None, "", "manual") or not loop.get("thread_id"):
        return []
    return store.thread_messages(loop["source"], loop["thread_id"])[-6:]


def _who(loop, msgs):
    other = next((m for m in reversed(msgs) if not m.is_from_me), None)
    name = loop.get("person") or (other.sender_name if other else "")
    address = other.sender if other else ""
    if not name:  # "Attend meeting with Mahan Agabegi" -> Mahan Agabegi
        m = re.search(r"\b(?:with|from|by|meet(?:ing)?)\s+((?:[A-Z][\w'-]+\s?){1,4})", loop["summary"])
        name = m.group(1).strip() if m else ""
    return name, address


def searches(name, org):
    q = " ".join(x for x in (name, org) if x)
    return [{"label": "Search the web", "url": f"https://www.google.com/search?q={quote_plus(q)}"},
            {"label": "Search LinkedIn", "url": f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(q)}"}]


def rule_explore(loop, name, address, msgs):
    org = _org_from(address)
    text = " ".join([loop["summary"]] + [f"{m.subject} {m.text}" for m in msgs]).lower()
    reasons = [r for pat, r in REASONS if re.search(pat, text)] or \
              ["The messages don't say. Ask them for a line on what they'd like to cover before you meet."]
    who = (f"{name} appears to work at {org}, judging by their email address." if name and org
           else f"{name}. Their email address doesn't show an employer." if name and address
           else f"{name}. There's no email from them in Loops, so this is from the name only." if name
           else "Couldn't tell who this is from the task.")
    return {"name": name, "who": who, "role": "", "org": org, "background": [], "likely_reasons": reasons,
            "prep": ["Look them up on LinkedIn with the button below", "Have one line ready on who you are and what you're looking for",
                     "Prepare two questions about their work"],
            "sources": [], "searched": False}


def explore(store, loop, use_ai=False):
    loop = dict(loop)
    msgs = _thread(store, loop)
    name, address = _who(loop, msgs)
    out = rule_explore(loop, name, address, msgs)
    if use_ai and (name or address):
        try:
            from .llm import ask_chat
            convo = "\n".join(f"[{m.ts:%a %d %b %H:%M}] {'Me' if m.is_from_me else m.sender_name}: {m.text[:700]}" for m in msgs)
            cv = ""
            try:
                r = store.db.execute("SELECT text FROM docs WHERE kind='cv' ORDER BY id DESC LIMIT 1").fetchone()
                cv = r["text"][:2500] if r else ""
            except Exception:
                pass
            txt = ask_chat(SYSTEM, [{"role": "user", "content":
                f"Task: {loop['summary']}\nPerson: {name or 'unknown'}\nTheir email: {address or 'unknown'}\n"
                f"Messages so far:\n{convo or '(none)'}\n\nMy CV (for context, may be empty):\n{cv}\n\n"
                'Search the web, then return {"who": "one sentence: who they are", "role": "current job title or empty", '
                '"org": "employer or empty", "background": ["up to 4 short points on their career or public work"], '
                '"likely_reasons": ["1 to 3 likely reasons for this meeting, most likely first, based on the messages and who they are"], '
                '"prep": ["up to 3 specific things to prepare or ask"], "sources": ["urls you used"], '
                '"unsure": "empty, or why you might have the wrong person"}'}], max_tokens=1400, search=True)
            m = re.search(r"\{.*\}", txt, re.S)
            d = json.loads(m.group(0)) if m else None
            if d and d.get("who"):
                out.update({k: d.get(k) or out.get(k) for k in ("who", "role", "org", "background", "likely_reasons", "prep")})
                out["sources"] = [u for u in (d.get("sources") or []) if str(u).startswith("http")][:5]
                out["unsure"] = d.get("unsure") or ""
                out["searched"] = True
        except Exception as e:
            print(f"[loops] explore fell back to rules: {e}")
    out["links"] = searches(name, out.get("org"))
    store.db.execute("INSERT INTO summaries(loop_id,kind,body,created) VALUES(?,?,?,?)",
                     (loop["id"], "explore", json.dumps(out), now_iso()))
    store.db.commit()
    return out


def cached(store, loop_id):
    r = store.db.execute("SELECT body FROM summaries WHERE loop_id=? AND kind='explore' ORDER BY id DESC LIMIT 1",
                         (loop_id,)).fetchone()
    return json.loads(r["body"]) if r else None
