"""Decision engine: every yes/no, choice and score goes through here.

Jev is the target backend (typed answers with calibrated probabilities).
Until you have Jev API access, ClaudeDecisions stands in. Note: Claude's
self-reported probabilities are NOT well calibrated, so treat thresholds as
provisional and check them against the decisions log.
"""
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
        return JevDecisions()
    return ClaudeDecisions()
