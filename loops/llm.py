"""Thin Claude wrapper that returns parsed JSON."""
import json
import re

import anthropic

from .config import ANTHROPIC_MODEL

_client = None

def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def ask_json(system: str, user: str, max_tokens: int = 700, model: str = "") -> dict:
    r = client().messages.create(model=model or ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in r.content if b.type == "text")
    m = re.search(r"\{.*\}", re.sub(r"```(?:json)?", "", text), re.S)
    if not m:
        raise ValueError(f"No JSON in model reply: {text[:200]}")
    return json.loads(m.group(0))


def ask_text(system: str, user: str, max_tokens: int = 500) -> str:
    r = client().messages.create(model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text").strip()


def ask_chat(system: str, messages: list[dict], max_tokens: int = 900, search: bool = False) -> str:
    """Multi-turn answer. With search=True Claude may look things up on the web (server-side tool)."""
    kw = {}
    if search:
        kw["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    try:
        r = client().messages.create(model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
                                     messages=messages, **kw)
    except Exception:
        if not search:
            raise
        r = client().messages.create(model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system, messages=messages)
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
