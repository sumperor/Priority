"""Loops backend CLI.

  python run.py auth gmail|outlook|slack   connect an account (once)
  python run.py sync                        pull messages, find loops, auto-close
  python run.py list                        ranked open loops
  python run.py close ID [--outcome well|partly|no] [--hours 1.5]
  python run.py confirm ID                  accept a 'probably done' close
  python run.py reject ID                   it wasn't done, keep chasing
  python run.py snooze ID                   push back a day (raises priority)
  python run.py brief [--voice] [--send]    morning brief
  python run.py serve                       open the app at http://127.0.0.1:8765
"""
import argparse
from datetime import datetime, timedelta

from loops import config as C
from loops.connectors import ALL
from loops.decisions import get_engine
from loops.engine import ranked, sync
from loops.store import Store


def main():
    ap = argparse.ArgumentParser(description="Loops backend")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth"); a.add_argument("service", choices=list(ALL))
    sub.add_parser("sync"); sub.add_parser("list"); sub.add_parser("serve")
    c = sub.add_parser("close"); c.add_argument("id", type=int)
    c.add_argument("--outcome", default="well", choices=["well", "partly", "no"]); c.add_argument("--hours", type=float)
    for name in ("confirm", "reject", "snooze"):
        sub.add_parser(name).add_argument("id", type=int)
    b = sub.add_parser("brief"); b.add_argument("--voice", action="store_true"); b.add_argument("--send", action="store_true")
    args = ap.parse_args()

    store = Store(C.DB_PATH)

    if args.cmd == "serve":
        from loops.server import serve
        serve()

    elif args.cmd == "auth":
        ALL[args.service]().authenticate()

    elif args.cmd == "sync":
        sync(store, [ALL[n]() for n in C.CONNECTORS if n in ALL], get_engine())

    elif args.cmd == "list":
        rows = ranked(store)
        if not rows:
            print("No open loops.")
        for r in rows:
            flag = "  [probably done: confirm/reject]" if r["status"] == "pending_close" else ""
            print(f"#{r['id']:<4} {r['bucket']:<7} {r['summary']}  ({r['person']}, {r['source']}, due {r['due'][:16]})"
                  f"  EV/h {r['ev_per_hour']:.0f}, P(miss) {r['p_miss']:.0%}{flag}")

    elif args.cmd == "close":
        store.close_loop(args.id, outcome=args.outcome, actual_h=args.hours)
        print("Closed.")

    elif args.cmd == "confirm":
        store.close_loop(args.id, outcome="auto_confirmed", confidence=store.get_loop(args.id)["close_confidence"])
        store.db.execute("UPDATE decisions SET confirmed=1 WHERE id=(SELECT MAX(id) FROM decisions WHERE loop_id=?)", (args.id,))
        store.db.commit(); print("Confirmed and closed.")

    elif args.cmd == "reject":
        store.update_loop(args.id, status="open")
        store.db.execute("UPDATE decisions SET confirmed=0 WHERE id=(SELECT MAX(id) FROM decisions WHERE loop_id=?)", (args.id,))
        store.db.commit(); print("Kept open.")

    elif args.cmd == "snooze":
        loop = store.get_loop(args.id)
        due = max(datetime.fromisoformat(loop["due"]), datetime.now(datetime.fromisoformat(loop["due"]).tzinfo))
        store.update_loop(args.id, due=(due + timedelta(days=1)).isoformat(), snoozes=(loop["snoozes"] or 0) + 1)
        print("Pushed back a day.")

    elif args.cmd == "brief":
        from loops.brief import send_telegram, tts, write_brief
        text = write_brief(store)
        print(text)
        audio = tts(text) if args.voice else None
        if args.send:
            send_telegram(text, audio)
            print("Sent to Telegram.")


if __name__ == "__main__":
    main()
