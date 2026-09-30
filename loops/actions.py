"""What an email from a company is asking you to do, and the right question when you tick it off.

"Your Claude credits are running low" is not something to reply to; it's "top up your credits".
"Update your Netflix payment method" is "did you update the payment method?". This reads the
subject and body with rules (no API key needed) and returns the task, the close question and the
link that takes you to do it.
"""
import re

URL = re.compile(r"https?://[^\s<>\"')\]]+")
NOREPLY = re.compile(r"(no-?reply|do-?not-?reply|donotreply|notifications?|alerts?|billing|accounts?|info|support|"
                     r"hello|team|news|updates?|service|mailer|automated|system)@", re.I)

# (key, pattern, task, close question, follow-ups, words that mark the link to do it)
RULES = [
    ("payment", r"payment (failed|declined|was unsuccessful|didn'?t go through|issue|problem)|update (your )?(payment|billing|card)|"
                r"card (has )?(expired|expiring|declined)|billing (issue|problem|information|details)|problem with your (payment|billing)|"
                r"change (your )?(billing|payment) (type|method|plan)",
     "Update your payment details for {org}", "Did you update your payment details for {org}?",
     [{"question": "What did you change?", "kind": "choice",
       "options": ["Updated the card", "Changed the payment type", "Changed the plan", "Cancelled it"], "use": "note"}],
     r"update|payment|billing|card|account|manage"),
    ("credits", r"credits? (are |is )?(running )?(low|out|exhausted|almost|nearly|below)|out of credits|credit balance|"
                r"usage (limit|threshold|alert)|(approaching|reached|exceeded) (your )?(usage|spend(ing)?|credit|quota) limit|"
                r"low balance|top[- ]?up|recharge|add (more )?(funds|credits)",
     "Top up your {org} credits", "Did you top up your {org} credits?",
     [{"question": "Did you change your credit limit or turn on auto top-up?", "kind": "choice",
       "options": ["Changed the limit", "Turned on auto top-up", "Neither, not needed"], "use": "note"}],
     r"billing|credits?|top.?up|add funds|buy|plans?|usage|console|account"),
    ("renew", r"(renew|renewal)|(subscription|membership|plan|licen[cs]e|passport|insurance|domain|railcard) "
              r"(is )?(expir(es|ing)|ends|ending|due)|(expir(es|ing)) (soon|on|in)",
     "Renew your {thing}", "Did you renew your {thing}?",
     [{"question": "Did the renewal price change?", "kind": "choice", "options": ["Same price", "It went up", "I cancelled"], "use": "note"}],
     r"renew|manage|subscription|account"),
    ("bill", r"(invoice|bill|statement) (is )?(due|ready|available|overdue)|amount due|payment (is )?(due|overdue)|"
             r"(rent|council tax|tuition|fees?) (is )?due|outstanding balance|final reminder",
     "Pay the {org} bill", "Did you pay the {org} bill?", [],
     r"pay|invoice|bill|view|account"),
    ("verify", r"verify (your )?(email|account|identity|phone)|confirm (your )?(email|account|address|identity)|"
               r"activate (your )?account|complete (your )?(registration|sign.?up|verification)",
     "Verify your {org} account", "Did you verify your {org} account?", [],
     r"verify|confirm|activate|complete"),
    ("security", r"security alert|unusual (sign.?in|activity|login)|new (sign.?in|login|device)|suspicious|"
                 r"reset your password|password (was )?changed|someone (tried|signed)",
     "Check the {org} security alert", "Did you check it was you, or secure your {org} account?",
     [{"question": "Was it you?", "kind": "choice", "options": ["Yes, it was me", "No, I changed my password"], "use": "note"}],
     r"review|secure|check|password|activity|account"),
    ("parcel", r"ready (for|to) (collect|collection|pick.?up)|collect your (parcel|order|package)|missed (your )?delivery|"
               r"(rearrange|reschedule) (your )?delivery|we tried to deliver|awaiting collection",
     "Collect your parcel from {org}", "Did you collect your parcel?", [],
     r"track|rearrange|collect|delivery|manage"),
    ("form", r"action (is )?required|action needed|please (complete|sign|submit|fill)|sign (the |your )?(document|contract|agreement)|"
             r"docusign|complete (your|the) (form|profile|survey|questionnaire|details)|submit (your|the) (documents?|details|form)|"
             r"respond by|deadline to (respond|submit)",
     "{subject}", "Did you do what {org} asked?", [],
     r"complete|sign|submit|review|start|open|continue|respond|view"),
]
_COMPILED = [(k, re.compile(p, re.I), t, q, f, re.compile(w, re.I)) for k, p, t, q, f, w in RULES]
BAD_LINK = re.compile(r"unsubscribe|preferences|privacy|terms|help|support\.|facebook|twitter|//(www\.)?x\.com|instagram|linkedin\.com/company|"
                      r"youtube|tiktok|apps\.apple|play\.google|\.(png|jpe?g|gif|svg)(\?|$)|mailto:|view.?in.?browser|pixel|open\.aspx", re.I)


def org_of(m):
    name = (m.sender_name or "").split("<")[0].strip().strip('"')
    name = re.sub(r"\s*(team|support|billing|notifications?|no-?reply|accounts?|payments?)\s*$", "", name, flags=re.I).strip()
    if name and "@" not in name:
        return name
    dom = (m.sender or "").split("@")[-1].split(".")
    return (dom[-3] if len(dom) > 2 and dom[-2] in ("co", "com", "org", "ac") else dom[-2] if len(dom) > 1 else dom[0]).title()


def is_automated(m):
    return bool(NOREPLY.search(m.sender or ""))


def links(text):
    """(anchor text, url) pairs, from the 'text url' lines the mail connectors produce, plus bare urls."""
    out = []
    for line in (text or "").splitlines():
        for u in URL.findall(line):
            u = u.rstrip(".,;")
            label = URL.sub("", line).strip(" :-|>")
            out.append((label, u))
    return out


def best_link(text, words, prefer_meeting=False):
    """The link that does the thing: a join link for meetings, else the button whose words match."""
    pairs = [(l, u) for l, u in links(text) if not BAD_LINK.search(u) and not BAD_LINK.search(l)]
    if prefer_meeting:
        for l, u in pairs:
            if re.search(r"(zoom\.us/j/|meet\.google\.com/|teams\.microsoft\.com/l/meetup|webex\.com/meet|whereby\.com)", u):
                return u
    rx = words if hasattr(words, "search") else re.compile(words, re.I)
    for l, u in pairs:
        if l and rx.search(l):
            return u
    for l, u in pairs:
        if rx.search(u):
            return u
    return pairs[0][1] if pairs else ""


def action_for(m):
    """{'key','summary','happened','followups','link'} when the email asks you to do something specific, else None."""
    hay = f"{m.subject}\n{(m.text or '')[:3000]}"
    for key, rx, task, q, fu, words in _COMPILED:
        if not rx.search(hay):
            continue
        org = org_of(m)
        subject = re.sub(r"^((re|fwd?):\s*)+", "", m.subject or "", flags=re.I).strip() or f"Deal with the {org} email"
        thing = "subscription"
        t = re.search(r"\b(subscription|membership|plan|licen[cs]e|passport|insurance|domain|railcard)\b", hay, re.I)
        if t:
            thing = f"{org} {t.group(1).lower()}"
        fill = dict(org=org, subject=subject[:90], thing=thing)
        return {"key": key, "summary": task.format(**fill), "happened": q.format(**fill),
                "followups": fu, "link": best_link(m.text, words)}
    return None


def questions(action_key, loop):
    """Close questions for a loop made from an action email (None if unknown)."""
    for key, rx, task, q, fu, words in _COMPILED:
        if key == action_key:
            org = loop.get("person") or "them"
            thing = (loop.get("summary") or "").replace("Renew your ", "") or "subscription"
            return {"happened": q.format(org=org, subject=loop.get("summary") or "", thing=thing), "followups": fu}
    return None
