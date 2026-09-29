"""Reminders that reach you without the browser: Mac notifications and phone push (ntfy).

Mac: sent by Sparrow itself while it's running. Uses `terminal-notifier` when installed (tapping the
notification opens the task), otherwise macOS's built-in notifications via osascript.
Phone: ntfy (ntfy.sh). Install the ntfy app, subscribe to your private topic. The reminder text passes
through ntfy's servers, so it's off until you turn it on.
"""
import json
import secrets
import shutil
import subprocess
import sys

import requests

from . import config as C

NTFY = "https://ntfy.sh"
last_error = {}


def settings():
    d = C.read_secret("notify.json")
    d.setdefault("mac", sys.platform == "darwin")
    d.setdefault("ntfy", False)
    d.setdefault("ntfy_topic", "")
    return d


def save(d):
    C.write_secret("notify.json", d)


def new_topic():
    # Unguessable, so nobody else can read your reminders on ntfy
    return "sparrow-" + "".join(c for c in secrets.token_urlsafe(12).lower() if c.isalnum())[:14]


def mac_available():
    return sys.platform == "darwin"


def send_mac(title, body, url=None):
    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-message", body, "-sound", "default", "-group", "sparrow"]
        if url:
            cmd += ["-open", url]
        subprocess.run(cmd, timeout=10, check=False, capture_output=True)
        return "terminal-notifier"
    script = f"display notification {json.dumps(body, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)} sound name \"Glass\""
    subprocess.run(["osascript", "-e", script], timeout=10, check=False, capture_output=True)
    return "osascript"


def send_ntfy(topic, title, body, tags="bird"):
    r = requests.post(f"{NTFY}/{topic}", data=body.encode("utf-8"), timeout=10,
                      headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags, "Priority": "high"})
    r.raise_for_status()


def send(title, body, url=None, done=False):
    """Deliver one reminder everywhere that's switched on. Never raises; errors are kept for the settings screen."""
    st, sent = settings(), []
    if st["mac"] and mac_available():
        try:
            sent.append(send_mac(title, body, url))
            last_error.pop("mac", None)
        except Exception as e:
            last_error["mac"] = str(e)[:200]
    if st["ntfy"] and st["ntfy_topic"]:
        try:
            send_ntfy(st["ntfy_topic"], title, body, tags="tada" if done else "bird")
            sent.append("ntfy")
            last_error.pop("ntfy", None)
        except Exception as e:
            last_error["ntfy"] = str(e)[:200]
    return sent


def dispatch(nudges):
    for n in nudges:
        done = n.get("kind") == "done"
        n["sent"] = send("Nice one" if done else "Sparrow", n["text"], url=f"http://127.0.0.1:{C.PORT}/#task-{n['id']}", done=done)
