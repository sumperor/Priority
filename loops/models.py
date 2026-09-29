from dataclasses import dataclass
from datetime import datetime


@dataclass
class Message:
    """One message from any source, normalised."""
    source: str          # gmail | outlook | slack
    msg_id: str
    thread_id: str
    sender: str          # email address or Slack user id
    sender_name: str
    is_from_me: bool
    text: str
    ts: datetime         # timezone-aware UTC
    subject: str = ""
