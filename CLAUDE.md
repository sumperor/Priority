# Sparrow

Sparrow (Python package `loops`; the app used to be called Loops). A local-first task-completion agent, currently focused on job hunters. It reads Gmail, Outlook and
Slack, turns messages and notes into "loops" (tasks), plans them by deadline and effort, chases the
user until they're done, and runs focused job-hunt sessions with a Claude-backed helper.

## Run and test
- Run: `python run.py serve` then open http://127.0.0.1:8765 (focus mode: /#focus)
- Test: `python -m pytest -q tests` (must pass before you finish any change)
- Python 3.10+, deps in `requirements.txt`. Secrets live in `.env` (never read it aloud, print it, or commit it)
- After a user-visible change, bump the version string in `loops/server.py` ("Sparrow vX.Y running")

## Layout
- `loops/server.py` FastAPI app and main API; `loops/session.py` job-hunt sessions (router)
- `loops/engine.py` sync, loop detection (incl. assessment emails), auto-close, EV ranking, `plan()` ordering
- `loops/forecast.py` latest safe start, cost-of-miss text, knock-on via `blocks`
- `loops/agent.py` chasing nudges, reply parsing ("in 10 minutes"), spoken corrections
- `loops/closing.py` task-specific close questions; `loops/extract.py` Claude extraction + `rule_parse` fallback
- `loops/decisions.py` yes/no decisions (Claude now, Jev stub); `loops/llm.py` Claude wrappers (incl. web search)
- `loops/store.py` SQLite schema, migrations in `_migrate()`, accuracy stats
- `loops/evidence.py` proof in the inbox that a loop is done (checked after sync and before every nudge)
- `loops/leads.py` job-alert and recruiter emails: credibility, fit, "worth applying"; `loops/interviews.py` write-ups
- `loops/areas.py` which section a loop lives in (rules only); `loops/explore.py` "Explore": who someone is, why you are meeting; `loops/invites.py` calendar invites become meetings
- `loops/jobs.py` jobs from the inbox: scan 14 days lists every job (HTML alerts too) without judging; "Check relevance" (one or all, background) reads the posting and compares with CV/LinkedIn/interests; firm research, tailored CV
- `loops/planner.py` the calendar: events, meetings, deadlines and planned task blocks in free time (08:00 to 22:00); drag keeps a time
- `loops/errands.py` errands: one question at a time via `next_step` (`GET /api/loops/{id}/next`): calendar day, free time, place from the note or asked, map with walk/cycle/drive minutes, time there, then the estimate; `loops/maps.py` travel time from where you are (browser location, else the Mac via CoreLocationCLI, else asked) to the nearest match on OpenStreetMap (walk, cycle, drive); Claude web search tidies misheard names if a key is set; asks for bus/train or if the lookup fails
- `tests/test_scenarios.py` 20 end-to-end edge cases; run them before shipping
- `loops/caller.py` last-resort phone call (ElevenLabs agent + Twilio): deadline close, 2 reminders ignored, not in an event/focus/quiet hours; transcript read back as a reply
- `loops/notify.py` reminders to Mac notifications and phone (ntfy), sent by a server timer
- `loops/static/vendor/motion.js` Motion 12 (animations); page falls back to built-in animations
- `loops/static/welcome.html` landing page (/welcome) with Motion scenes and pricing; `loops/static/login.html` + `loops/account.py` local accounts (hashed passwords), no checkout yet
- `loops/connect.py` Connect buttons: Google/Microsoft sign-in, Slack token, iMessage access, disconnect
- `loops/connectors/` gmail (All Mail, every tab; bulk mail only if actionable: bookings, orders, appointments), outlook, teams, slack, imessage (mail, chat, calendar); `loops/static/index.html` the whole UI, incl. focus mode (no build step)

## Rules
- Every Claude call must have a non-AI fallback; the app must work with no API key
- Numbers shown to the user (times, minutes short, pace) come from code, never from the model
- New DB columns go through `_migrate()` so existing user databases keep working
- UI copy: plain, short, no em dashes, no jargon. Importance is decided by the algorithm, never asked of the user
- Keep diffs small and targeted; read only the files the task needs

## Working with Sumedh
- Replies: lead with the main point, then short bullets. Be direct about what's broken or untested
- He dictates by voice, so expect phonetic spellings ("cheesing" means chasing)
- Before building something big, restate what you understood in 2 to 3 bullets
