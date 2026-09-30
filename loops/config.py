import os
from dotenv import load_dotenv

load_dotenv()

def _f(k, d): return float(os.getenv(k, d))

DB_PATH = os.getenv("LOOPS_DB", "loops.db")
# Which account types the app offers. Only the ones you connect in the app are actually read.
ALL_SOURCES = "gmail,outlook,teams,slack,imessage"
_c = os.getenv("CONNECTORS", ALL_SOURCES)
if _c.replace(" ", "") == "gmail,outlook,slack":
    _c = ALL_SOURCES  # the old .env default: offer every account type
CONNECTORS = [c.strip() for c in _c.split(",") if c.strip()]
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))
WAITING_DAYS = int(os.getenv("WAITING_DAYS", "3"))
AUTO_CLOSE = _f("AUTO_CLOSE_THRESHOLD", "0.9")
CONFIRM_CLOSE = _f("CONFIRM_THRESHOLD", "0.6")
DETECT_THRESHOLD = _f("DETECT_THRESHOLD", "0.6")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
# Reading every email is high volume and simple: a small, fast model keeps it cheap
READER_MODEL = os.getenv("READER_MODEL", "claude-haiku-4-5")
DECISION_BACKEND = os.getenv("DECISION_BACKEND", "claude")
JEV_API_KEY = os.getenv("JEV_API_KEY", "")

GMAIL_CREDENTIALS = os.getenv("GMAIL_CREDENTIALS", "secrets/gmail_credentials.json")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN", "secrets/gmail_token.json")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_TOKEN_CACHE = os.getenv("MS_TOKEN_CACHE", "secrets/ms_token_cache.json")
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "")
SECRETS_DIR = os.getenv("LOOPS_SECRETS", "secrets")


def secret_path(name):
    return os.path.join(SECRETS_DIR, name)


def read_secret(name):
    import json
    try:
        with open(secret_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_secret(name, data):
    import json
    os.makedirs(SECRETS_DIR, exist_ok=True)
    path = secret_path(name)
    with open(path, "w") as f:
        json.dump(data, f)
    try:
        os.chmod(path, 0o600)  # only you can read it
    except OSError:
        pass


def ms_client_id():
    """Microsoft app ID: from .env, or saved from the Connect screen."""
    return os.getenv("MS_CLIENT_ID") or read_secret("microsoft_app.json").get("client_id", "")


def slack_token():
    return os.getenv("SLACK_USER_TOKEN") or read_secret("slack.json").get("token", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# Default hours until a loop is due, by type, when the message gives no date
DEFAULT_DUE_HOURS = {"reply": 24, "promise": 72, "waiting": 72, "task": 48, "call": 24, "assessment": 120}
AUTO_SYNC_MINUTES = int(os.getenv("AUTO_SYNC_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8765"))

# Chasing: how often to nag once a loop should have started, and how early to give a heads-up
CHASE_EVERY_MINUTES = float(os.getenv("CHASE_EVERY_MINUTES", "2"))
HEADS_UP_MINUTES = float(os.getenv("HEADS_UP_MINUTES", "15"))
REMIND_EVERY_MINUTES = float(os.getenv("REMIND_EVERY_MINUTES", "60"))   # gentle reminder about the next thing, at most this often
TASK_REMIND_HOURS = float(os.getenv("TASK_REMIND_HOURS", "3"))          # the same task comes up again after this long
OVERDUE_EVERY_HOURS = float(os.getenv("OVERDUE_EVERY_HOURS", "3"))
