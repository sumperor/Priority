"""Which part of life a loop belongs to. Decided by code, never asked of the user.
Sections on the page are built from these, and only appear while they have something in them."""
import re

AREAS = ["Jobs", "Study", "Work", "Money and admin", "Personal", "Other"]

RULES = [
    ("Jobs", r"\b(appl(y|ied|ication)s?|interview\w*|assessment|hirevue|shl|codility|hackerrank|pymetrics|recruit\w*|"
             r"cv|resume|cover letter|job|role|offer|graduate scheme|internship|hear back|psychometric|"
             r"numerical reasoning|verbal reasoning|thank-you note|careers?)\b"),
    ("Study", r"\b(exam|revis\w*|study|studying|assignment|essay|coursework|dissertation|lecture|seminar|module|"
              r"tutor\w*|homework|course|uni|university|class|thesis|learn\w*|quiz|reading list)\b"),
    ("Money and admin", r"\b(bill|pay|paid|payment|rent|bank|tax|invoice|insurance|visa|passport|council|"
                        r"subscription|refund|form|doctor|gp|dentist|appointment|renew\w*|landlord|mortgage|loan)\b"),
    ("Work", r"\b(client|meeting|deck|report|manager|boss|team|project|stakeholder|pitch|demo|standup|"
             r"slides|proposal|contract|colleague|sprint|review)\b"),
    ("Personal", r"\b(groceries|grocery|shopping|shop for|supermarket|cook|dinner|laundry|clean(ing)?|haircut|pharmacy|mum|mom|dad|mother|father|sister|brother|friend|birthday|gym|party|dinner|family|"
                 r"partner|wedding|holiday|trip|flight|train|call \w+ back|ring)\b"),
]


def classify(summary="", stakes="", person="", source="", type_=""):
    if type_ == "assessment":
        return "Jobs"
    text = f"{summary} {stakes}".lower()
    for area, pat in RULES:
        if re.search(pat, text):
            return area
    if source == "slack":
        return "Work"
    return "Other"
