from .gmail import GmailConnector
from .imessage import IMessageConnector
from .outlook import OutlookConnector
from .slack import SlackConnector
from .teams import TeamsConnector

ALL = {"gmail": GmailConnector, "outlook": OutlookConnector, "teams": TeamsConnector,
       "slack": SlackConnector, "imessage": IMessageConnector}
LABELS = {"gmail": "Gmail", "outlook": "Outlook", "teams": "Teams", "slack": "Slack", "imessage": "iMessage"}
