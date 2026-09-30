# Handoff: put Sparrow on sumedhgarimella.com with phone reminders

Written by the cloud Claude session so a local session on Sumedh's Mac can carry on. Read CLAUDE.md too.

## Where things are
- App is at v0.38 on branch `claude/new-session-0em830` (pushed). 79 tests pass.
- v0.38 fixed chasing: hourly gentle reminders for non-urgent and no-deadline tasks, same task every 3h,
  past deadlines asked 3 times, nothing in quiet hours (22:00 to 08:00, from the call settings).
- Sumedh still gets no notifications. Delivery channels today (`loops/notify.py`):
  Mac (osascript, often silently blocked; `brew install terminal-notifier` is more reliable),
  ntfy phone push (off by default), browser Notification (only while a Sparrow tab is open).

## What he wants
- Reminders on his phone without asking people to install ntfy.
- Sparrow reachable at a subdomain of sumedhgarimella.com, using Cloudflare (the domain is on his Cloudflare account).

## The plan agreed
1. Cloudflare Access FIRST (Sparrow has no real login and reads Gmail): Zero Trust > Access > Applications >
   Self-hosted, domain `sparrow.sumedhgarimella.com`, policy Allow, Emails, his Gmail address.
2. Cloudflare Tunnel on the Mac (Sparrow keeps running locally, data stays on the Mac):
   ```
   brew install cloudflared
   cloudflared tunnel login
   cloudflared tunnel create sparrow
   cloudflared tunnel route dns sparrow sparrow.sumedhgarimella.com
   cloudflared tunnel run --url http://localhost:8765 sparrow
   ```
   with `python run.py serve` running. Check the phone gets the Access login before the app.
   Later: run cloudflared as a service (`sudo cloudflared service install`) and keep the Mac awake.
3. Then build v0.39: installable web app (manifest, icon, service worker) + Web Push (VAPID keys in
   `.env`, subscriptions stored via `_migrate()`, `notify.send` also pushes to every subscription,
   e.g. with `pywebpush`). iPhone needs "Add to Home Screen" first (iOS 16.4+). Add a "Phone" toggle in
   the reminders sheet in `loops/static/index.html`. No API key needed, so no Claude fallback issue.
4. Keep Gmail/Outlook connect on `127.0.0.1` (OAuth redirects are set up for localhost).

## Not in scope yet
- Other users: needs per-user accounts and data on a real server. Later, with real auth and payments.
- SMS escalation via Twilio (about 4p a text) was offered as an option; not chosen.
