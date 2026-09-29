import os
from dotenv import load_dotenv

load_dotenv()

def _f(k, d): return float(os.getenv(k, d))

DB_PATH = os.getenv("LOOPS_DB", "loops.db")
CONNECTORS = [c.strip() for c in os.getenv("CONNECTORS", "gmail,outlook,slack").split(",") if c.strip()]
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))
WAITING_DAYS = int(os.getenv("WAITING_DAYS", "3"))
AUTO_CLOSE = _f("AUTO_CLOSE_THRESHOLD", "0.9")
CONFIRM_CLOSE = _f("CONFIRM_THRESHOLD", "0.6")
DETECT_THRESHOLD = _f("DETECT_THRESHOLD", "0.6")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
DECISION_BACKEND = os.getenv("DECISION_BACKEND", "claude")
JEV_API_KEY = os.getenv("JEV_API_KEY", "")

GMAIL_CREDENTIALS = os.getenv("GMAIL_CREDENTIALS", "secrets/gmail_credentials.json")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN", "secrets/gmail_token.json")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TOKEN_CACHE = os.getenv("MS_TOKEN_CACHE", "secrets/ms_token_cache.json")
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# Default hours until a loop is due, by type, when the message gives no date
DEFAULT_DUE_HOURS = {"reply": 24, "promise": 72, "waiting": 72, "task": 48, "call": 24, "assessment": 120}
AUTO_SYNC_MINUTES = int(os.getenv("AUTO_SYNC_MINUTES", "15"))
PORT = int(os.getenv("PORT", "8765"))

# Chasing: how often to nag once a loop should have started, and how early to give a heads-up
CHASE_EVERY_MINUTES = float(os.getenv("CHASE_EVERY_MINUTES", "2"))
HEADS_UP_MINUTES = float(os.getenv("HEADS_UP_MINUTES", "15"))
