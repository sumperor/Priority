# Loops backend

Reads Gmail, Outlook and Slack, finds open loops (replies you owe, promises you made,
people you're waiting on), closes them automatically when evidence appears, ranks them by
expected value, and sends a spoken morning brief to Telegram.

Pipeline: connectors -> local SQLite -> decisions (Claude now, Jev later) -> Claude extraction
-> EV ranking -> brief (Claude text, ElevenLabs voice, Telegram delivery).

## 1. Install
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env        # then fill it in
    python -m pytest -q tests   # offline check, no accounts needed

## 2. Connect accounts (do each once)

### Gmail
1. Google Cloud Console: create a project, enable the Gmail API.
2. OAuth consent screen: External, Testing mode, add your own address as a test user.
3. Credentials: create OAuth client ID, type Desktop app, download JSON to `secrets/gmail_credentials.json`.
4. `python run.py auth gmail` (browser opens, approve read-only access).
Testing mode is fine for you. A public launch needs Google verification plus a yearly security assessment.

### Outlook
1. Azure portal, App registrations, New registration. Supported accounts: any org directory and personal Microsoft accounts.
2. Authentication: enable "Allow public client flows".
3. API permissions: Microsoft Graph, delegated, `Mail.Read` and `User.Read`.
4. Put the Application (client) ID in `MS_CLIENT_ID`, then `python run.py auth outlook` and follow the code prompt.
Work or university accounts may block this until an IT admin approves the app.

### Slack
1. api.slack.com/apps, Create app, From scratch, pick your workspace.
2. OAuth & Permissions, User Token Scopes: `im:history`, `im:read`, `mpim:history`, `mpim:read`, `users:read`.
3. Install to workspace, copy the User OAuth Token (xoxp-) into `SLACK_USER_TOKEN`.
4. `python run.py auth slack`.
Slack restricts history access and AI use of data for apps outside its Marketplace. Check current terms.

### Brief delivery
- Telegram: message @BotFather, `/newbot`, copy the token. Send your bot a message, then read your chat id from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`.
- ElevenLabs: API key and a voice ID from your voice library.

## 3. Open the app
    python run.py serve        # then open http://127.0.0.1:8765

Runs only on your machine (bound to 127.0.0.1). It checks your accounts every 5 minutes
(`AUTO_SYNC_MINUTES`, 0 to turn off) and gives you:
- **One page.** "Do this next" at the top, then a section per area (Jobs, Study, Work, Money and admin,
  Personal, Other). Areas are decided by code. A section appears when it has something in it and folds away when empty.
- **Check before chasing.** Before any reminder, it looks for proof in your inbox: an application confirmation,
  an assessment completion email, a next-round invite, a reply you sent. If it finds it, the task is ticked off
  and you get a "Nice one" card with the email it saw, plus **Not done yet** (tell it what's left and by when).
- **Job leads.** Job-alert and recruiter emails are checked for scam signs (fees, WhatsApp, personal email
  addresses, hidden links) and scored against your CV and the roles you've gone for. Good fits show up in Jobs
  with **Apply** and **Skip**. With an API key, Claude reads the job and the company for up to 3 new leads per check.
- **Interview write-ups.** When you close an interview, give rough notes and it writes a summary, the most
  interesting part, follow-ups and a thank-you note, and adds "Send a thank-you note" to your list.
- **No API key?** Everything above still works on rules; Claude makes it sharper.
- **Open / Waiting / To confirm / Closed** lists, ranked by value per hour
- **A forecast on every loop:** when to start to be safe, what happens if it's missed,
  what a 1-day slip means, and anything else it holds up (set "Missing this holds up" in Edit)
- **Feedback on close:** how it went, whether it mattered more or less than expected, how long it took
- **Accuracy tab:** your on-time rate and how often detection and auto-close were right (with Brier scores)
- **Capture** by typing or dictating, and **Hear brief** read aloud

## Focus sessions (job hunting)
Press **Focus** at the top of the page (or open http://127.0.0.1:8765/#focus).
- **Set objectives:** a few one-tap questions (goal, how many, which roles, how long, work rhythm).
- **Focus blocks:** 25/5 or 50/10 timer with breaks, water and food reminders, and a check-in after each block
  ("Good", "Stuck", "Tired", "Done for today").
- **Applications:** paste a job link or description. With your API key it reads the job, researches the company
  on the web, and shows what they want most, where your CV matches, gaps, and points for "why this company".
  Mark it submitted and it's added to your Jobs list as waiting to hear back; their reply closes it.
- **Helper:** ask anything mid-session, typed or spoken. Upload your CV once so answers are tailored.
- **Around you:** assessment emails become high-stakes loops (HireVue, SHL, numerical reasoning and so on);
  your calendar shows what's coming; after a good block it offers to slot an assessment in next.
- Normal reminders pause during a session so you're not nagged while focused.
- Calendar needs one re-connect after this update: `python run.py auth gmail` and/or `python run.py auth outlook`.

## Chasing
- Tap **Turn on reminders** once and keep the Loops tab open (it can be in the background).
- 15 minutes before something must start, you get a heads-up. Once it should have started,
  it nudges you every 2 minutes (`CHASE_EVERY_MINUTES`) until you reply.
- Reply in the check-in ("in 10 minutes", "at 2pm", "starting now", "done"). It works out whether
  that still leaves enough time and tells you, then waits until the time you gave.

## 4. Command line (optional)
    python run.py sync                 # pull, detect, auto-close
    python run.py list                 # ranked loops with EV per hour and P(miss)
    python run.py close 12 --outcome well --hours 0.5
    python run.py confirm 7            # accept a "probably done"
    python run.py reject 7             # not done, keep chasing
    python run.py snooze 4
    python run.py brief --voice --send

Automate (macOS/Linux cron):
    */15 * * * *  cd /path/to/loops-backend && .venv/bin/python run.py sync
    0 8 * * *     cd /path/to/loops-backend && .venv/bin/python run.py brief --voice --send

## How decisions work
- Detect: one yes/no per new last message ("does this need a reply from me?"). Never re-asked.
- Auto-close: evidence after the triggering message is scored. Above 0.9 closes (0.95 for costly loops),
  0.6 to 0.9 asks you to confirm, below keeps chasing.
- Priority: P(miss) x cost / effort. Irreversible loops with cost 70+ always rank first. Top 3 are "Do now".
- Forecast: latest safe start = deadline minus effort x 1.5 minus 1 hour. Knock-on follows the "holds up" chain,
  and a blocking loop carries half the cost of what it blocks.
- Importance learning: after 5+ "mattered more/less" answers per consequence type, costs are scaled to match.
- Learning: your confirm/reject answers are logged against each probability in the `decisions` table,
  so you can measure accuracy (e.g. Brier score). `--hours` on close trains your personal effort multiplier.

## Switching to Jev
Set `DECISION_BACKEND=jev` and implement the three methods in `loops/decisions.py` from TypeSafe's API docs
(yes_no -> Noul, choice -> Choice, score -> Score). Everything else stays the same.

## Honest limits
- Message excerpts are sent to Claude (and later Jev) for decisions. Storage is local; processing is not yet.
- Claude's probabilities are not well calibrated. Treat thresholds as provisional until your logs show accuracy.
- Promises fulfilled in a different thread (new email with the file) won't auto-close; confirm them manually.
- Slack channel @mentions aren't read yet, only DMs and group DMs.
