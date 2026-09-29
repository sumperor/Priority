"""Morning brief: Claude writes it, ElevenLabs speaks it, Telegram delivers it."""
from datetime import datetime, timezone

import requests

from . import config as C
from .engine import ranked


def brief_facts(store):
    rows = ranked(store)
    now = datetime.now(timezone.utc)
    overdue = [r for r in rows if datetime.fromisoformat(r["due"]) < now]
    confirm = [r for r in rows if r["status"] == "pending_close"]
    lines = [f"Open loops: {len(rows)}. Overdue: {len(overdue)}. Waiting for your confirmation: {len(confirm)}."]
    for r in rows[:5]:
        lines.append(f"- [{r['bucket']}] {r['summary']} ({r['type']}, {r['person']}, due {r['due'][:16]}). "
                     f"If missed: {r['stakes']}")
    for r in confirm[:3]:
        lines.append(f"- Probably done, confirm: {r['summary']}")
    return "\n".join(lines)


def write_brief(store):
    facts = brief_facts(store)
    try:
        from .llm import ask_text
        return ask_text(
            "Write a spoken morning brief under 90 words. Plain sentences, no lists, no markdown, "
            "no em dashes. Lead with what to do first. Mention overdue items plainly.", facts)
    except Exception:
        return facts


def tts(text) -> bytes:
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{C.ELEVENLABS_VOICE_ID}",
        headers={"xi-api-key": C.ELEVENLABS_API_KEY, "Accept": "audio/mpeg"},
        json={"text": text, "model_id": C.ELEVENLABS_MODEL}, timeout=60)
    r.raise_for_status()
    return r.content


def send_telegram(text, audio: bytes | None = None):
    base = f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}"
    requests.post(f"{base}/sendMessage", data={"chat_id": C.TELEGRAM_CHAT_ID, "text": text}, timeout=30).raise_for_status()
    if audio:
        requests.post(f"{base}/sendAudio", data={"chat_id": C.TELEGRAM_CHAT_ID, "title": "Morning brief"},
                      files={"audio": ("brief.mp3", audio, "audio/mpeg")}, timeout=60).raise_for_status()
