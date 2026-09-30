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
- `loops/reader.py` one reading per new email: Claude (READER_MODEL, default claude-haiku-4-5) returns kind/task/when/link/close question; links must appear in the email; every decision (Claude or rules) saved in `readings` for the review screen (`/api/review`); corrections (`/api/loops/{id}/wrong`, `/api/review/...`) fix the task, mute senders marked "nothing to do", and are shown to Claude as examples. Gmail connector reads .ics attachments, strips quoted replies/signatures, keeps bulk mail (short) when a key is set
- `loops/actions.py` company/no-reply emails become the thing they ask for (top up credits, update payment, renew, pay, verify, security, parcel, forms) with the link to do it and the right close question; `loops/closing.py` task-specific close questions; `loops/extract.py` Claude extraction + `rule_parse` fallback
- `loops/decisions.py` yes/no decisions (Claude now, Jev stub); `loops/llm.py` Claude wrappers (incl. web search)
- `loops/store.py` SQLite schema, migrations in `_migrate()`, accuracy stats
- `loops/evidence.py` proof in the inbox that a loop is done (checked after sync and before every nudge)
- `loops/leads.py` job-alert and recruiter emails: credibility, fit, "worth applying"; `loops/interviews.py` write-ups
- `loops/areas.py` which section a loop lives in (rules only); `loops/explore.py` "Explore": who someone is, why you are meeting; `loops/invites.py` calendar invites, bookings/confirmations and "can we meet Thursday at 3?" emails become meetings (`when_loose` reads everyday dates)
- `loops/jobs.py` jobs from the inbox: scan 14 days lists every job (HTML alerts too) without judging; "Check relevance" (one or all, background) reads the posting and compares with CV/LinkedIn/interests; firm research, tailored CV
- `loops/planner.py` the calendar: events, meetings, deadlines and planned task blocks in free time (08:00 to 22:00), placed earliest real deadline first (`due_guess` tasks, whose deadline was a default, go after and show "No deadline"); drag keeps a time
- `loops/errands.py` errands: one question at a time via `next_step` (`GET /api/loops/{id}/next`): calendar day, free time, place from the note or asked, map with walk/cycle/drive minutes, time there, then the estimate; `loops/maps.py` travel time from where you are (browser location, else the Mac via CoreLocationCLI, else asked) to the nearest match on OpenStreetMap (walk, cycle, drive); Claude web search tidies misheard names if a key is set; asks for bus/train or if the lookup fails
- `tests/test_scenarios.py` 20 end-to-end edge cases; run them before shipping
- `loops/caller.py` last-resort phone call (ElevenLabs agent + Twilio): deadline close, 2 reminders ignored, not in an event/focus/quiet hours; transcript read back as a reply
- `loops/notify.py` reminders to Mac notifications and phone (ntfy), sent by a server timer
- `loops/static/vendor/motion.js` Motion 12 (animations); page falls back to built-in animations
- `loops/static/welcome.html` landing page (/welcome), "paper planner" art direction: paper grain, Fraunces + Source Serif + Caveat handwriting + Plex Mono, no gradients/blobs/glass/pills. GSAP (bundled in `loops/static/vendor/gsap/`) writes, ticks, strikes and stamps; one pinned scroll story on a notebook page; index-card pricing; static fallback without JS or with reduced motion. Design skill: `.claude/skills/ui-ux-pro-max`; `loops/static/login.html` + `loops/account.py` local accounts (hashed passwords), no checkout yet
- `loops/connect.py` Connect buttons: Google/Microsoft sign-in, Slack token, iMessage access, disconnect
- `loops/connectors/` gmail (All Mail, every tab; bulk mail only if actionable: bookings, orders, appointments), outlook, teams, slack, imessage (mail, chat, calendar); `loops/static/index.html` the whole UI, incl. focus mode (no build step); styled in the same paper planner theme as /welcome (theme block at the end of its <style>). Login/payment pages untouched until real auth and payments are added

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
- After any change he can try, end the reply with the run code block: `cd ~/loops`, `source .venv/bin/activate`, `git pull`, `pip install -r requirements.txt`, `python run.py serve`, plus the URL to open and the version to expect
