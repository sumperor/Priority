"""After an interview: a short write-up from your notes, plus a thank-you note draft.
Claude writes it when a key is set; otherwise your notes are tidied into the same shape by rules."""
import json
import re

from .store import now_iso

INTEREST = re.compile(r"\b(interest|excit|enjoy|liked|loved|talked about|discussed|asked (me )?about|told me|mentioned|focus)", re.I)
ACTION = re.compile(r"\b(send|follow up|next (step|round|stage)|will (get|come) back|by (monday|tuesday|wednesday|thursday|friday|next week)|"
                    r"hear back|deadline|need to)\b", re.I)


def _sentences(notes):
    parts = re.split(r"(?<=[.!?])\s+|\n+|;\s*", notes or "")
    return [p.strip(" -•*").rstrip(".") for p in parts if len(p.strip()) > 3]


def _research(store, org):
    try:
        r = store.db.execute("SELECT role, research FROM applications WHERE company LIKE ? ORDER BY id DESC LIMIT 1",
                             (f"%{org}%",)).fetchone()
        return (r["role"], json.loads(r["research"]) if r and r["research"] else None) if r else ("", None)
    except Exception:
        return "", None


def rule_draft(org, person, role, notes):
    s = _sentences(notes)
    highlight = next((x for x in s if INTEREST.search(x)), s[0] if s else "")
    follow = [x for x in s if ACTION.search(x)][:3]
    about = highlight[:1].lower() + highlight[1:] if highlight else "the team and the work you're doing"
    about = re.sub(r"^(we |they |she |he |i )?(talked|discussed|spoke) about ", "", about, flags=re.I)
    thank = (f"Hi {person or 'there'},\n\nThank you for taking the time to speak with me about the "
             f"{role or 'role'}{f' at {org}' if org else ''}. I especially enjoyed our conversation about {about}. "
             f"It made me even more keen on the role.\n\nPlease let me know if there's anything else you need from me.\n\nBest wishes")
    return {"summary": s[:6], "highlight": highlight, "follow_ups": follow, "thank_you": thank}


def draft(store, loop, notes, use_ai=False):
    from .evidence import org_of
    loop = dict(loop)
    org = org_of(loop)
    role, research = _research(store, org) if org else ("", None)
    person = loop.get("person") if loop.get("person") and loop.get("person") != org else ""
    out = rule_draft(org, person, role, notes)
    if use_ai and (notes or "").strip():
        try:
            from .llm import ask_json
            d = ask_json(
                "You turn a job hunter's rough interview notes into a short write-up. Use only what the notes say; "
                "never invent details. Plain words, no em dashes. Reply ONLY with JSON.",
                f"Interview: {loop['summary']}\nCompany: {org or 'unknown'}\nRole: {role or 'unknown'}\n"
                f"Research we did earlier: {json.dumps(research) if research else 'none'}\n\nNotes:\n{notes}\n\n"
                'Return {"summary": ["3 to 6 bullets of what was covered"], "highlight": "the most interesting part, one sentence", '
                '"follow_ups": ["things to do next, if any"], "thank_you": "a warm 80 to 120 word thank-you email that mentions the highlight, '
                'signed off with Best wishes and no name"}')
            if d.get("summary") and d.get("thank_you"):
                out = {"summary": d["summary"][:6], "highlight": d.get("highlight", ""),
                       "follow_ups": (d.get("follow_ups") or [])[:3], "thank_you": d["thank_you"]}
        except Exception as e:
            print(f"[loops] interview write-up fell back to rules: {e}")
    out["title"] = f"{org} interview" if org else loop["summary"]
    store.db.execute("INSERT INTO summaries(loop_id,kind,body,created) VALUES(?,?,?,?)",
                     (loop["id"], "interview", json.dumps(out), now_iso()))
    store.db.commit()
    return out


def is_interview(loop):
    s = dict(loop)["summary"]
    return bool(re.search(r"\binterview\b", s, re.I)) and not re.search(r"\b(prep|prepare|preparing|practi[cs]e)\b", s, re.I)
