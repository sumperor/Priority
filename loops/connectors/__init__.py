from .gmail import GmailConnector
from .outlook import OutlookConnector
from .slack import SlackConnector

ALL = {"gmail": GmailConnector, "outlook": OutlookConnector, "slack": SlackConnector}
