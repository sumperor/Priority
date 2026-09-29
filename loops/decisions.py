"""Decision engine: every yes/no, choice and score goes through here.

Jev is the target backend (typed answers with calibrated probabilities).
Until you have Jev API access, ClaudeDecisions stands in. Note: Claude's
self-reported probabilities are NOT well calibrated, so treat thresholds as
provisional and check them against the decisions log.
"""
import os
import re
from typing import Protocol

from .config import DECISION_BACKEND, JEV_API_KEY
from .llm import ask_json

SYSTEM = ("You make one narrow decision about a person's messages. Reply ONLY with JSON. "
          "Probabilities must be honest: use values near 0.5 when unsure.")


class DecisionEngine(Protocol):
    name: str
    def yes_no(self, state: str, question: str) -> float: ...
    def choice(self, state: str, question: str, options: list[str]) -> tuple[str, float]: ...
    def score(self, state: str, question: str, lo: int, hi: int) -> float: ...


class ClaudeDecisions:
    name = "claude"

    def yes_no(self, state, question):
        d = ask_json(SYSTEM, f"STATE:\n{state}\n\nQUESTION: {question}\n"
                             'Return {"p_yes": number between 0 and 1}')
        return max(0.0, min(1.0, float(d["p_yes"])))

    def choice(self, state, question, options):
        d = ask_json(SYSTEM, f"STATE:\n{state}\n\nQUESTION: {question}\nOPTIONS: {options}\n"
                             'Return {"answer": one option exactly, "p": probability it is right}')
        ans = d["answer"] if d["answer"] in options else options[0]
        return ans, float(d.get("p", 0.5))

    def score(self, state, question, lo, hi):
        d = ask_json(SYSTEM, f"STATE:\n{state}\n\nQUESTION: {question}\n"
                             f'Return {{"score": integer from {lo} to {hi}}}')
        return max(lo, min(hi, float(d["score"])))


class RuleDecisions:
    """No API key: plain rules, deliberately cautious. Closes never go past 'confirm' on rules alone."""
    name = "rules"

    def yes_no(self, state, question):
        lines = [l for l in state.strip().splitlines() if l.strip()]
        last = lines[-1].lower() if lines else ""
        q = question.lower()
        if "need a reply" in q:
            if re.search(r"no-?reply|do not reply|donotreply|unsubscribe|newsletter", state.lower()):
                return 0.05
            asks = re.search(r"\?|\b(can you|could you|would you|please|let me know|are you free|what do you think|"
                             r"when can|are you able|do you have)\b", last)
            return 0.7 if asks else 0.15
        if "commit" in q:
            return 0.7 if re.search(r"\b(i'll|i will|will send|will get back|let me (check|get back)|i can send|"
                                    r"i'll get)\b", last) else 0.1
        if "waiting on" in q:
            return 0.65 if re.search(r"\?|\b(can you|could you|please send|let me know)\b", last) else 0.15
        if "deliver" in q:
            return 0.15 if re.search(r"(get back to you|will send|i'll send|looking into|soon)", state.lower()) else 0.65
        if "complete" in q:
            return 0.65
        return 0.5

    def choice(self, state, question, options):
        return options[0], 0.34

    def score(self, state, question, lo, hi):
        return (lo + hi) / 2


class _WithFallback:
    """Claude first; any failure (no network, bad key, odd reply) falls back to rules."""
    def __init__(self, main):
        self.main, self.rules, self.name = main, RuleDecisions(), main.name

    def yes_no(self, state, question):
        try:
            return self.main.yes_no(state, question)
        except Exception as e:
            print(f"[loops] decision fell back to rules: {e}")
            return self.rules.yes_no(state, question)

    def choice(self, state, question, options):
        try:
            return self.main.choice(state, question, options)
        except Exception:
            return self.rules.choice(state, question, options)

    def score(self, state, question, lo, hi):
        try:
            return self.main.score(state, question, lo, hi)
        except Exception:
            return self.rules.score(state, question, lo, hi)


class JevDecisions:
    """Map to Jev's primitives once you have access:
       yes_no -> Noul, choice -> Choice, score -> Score.
       Jev answers many questions in one call, so batch them in production.
       Fill in from TypeSafe's API docs; left unimplemented rather than guessed."""
    name = "jev"

    def yes_no(self, state, question):
        raise NotImplementedError("Wire up Jev's Noul endpoint from TypeSafe's docs")

    def choice(self, state, question, options):
        raise NotImplementedError("Wire up Jev's Choice endpoint")

    def score(self, state, question, lo, hi):
        raise NotImplementedError("Wire up Jev's Score endpoint")


def get_engine() -> DecisionEngine:
    if DECISION_BACKEND == "jev" and JEV_API_KEY:
        return _WithFallback(JevDecisions())
    if not os.getenv("ANTHROPIC_API_KEY"):
        return RuleDecisions()
    return _WithFallback(ClaudeDecisions())
