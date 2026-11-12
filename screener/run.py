"""Run the screener.

  python -m screener.run --demo          # load the test pitches into a local db (needs ANTHROPIC_API_KEY)
  python -m screener.run --gmail         # poll the Gmail pitch label every 2 minutes
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from .llm import ClaudeLLM
from .pipeline import Screener
from .store import Store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--gmail", action="store_true")
    ap.add_argument("--every", type=int, default=120)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    store = Store(os.environ.get("SCREENER_DB", "screener.db"))

    if a.demo:
        from fixtures.pitches import cases
        s = Screener(store, ClaudeLLM())
        for c in cases():
            if c["fake"] == "BROKEN":
                continue
            store.set_rule("autonomy_level", str(c["expect"].get("level", 1)))
            print(c["id"], c["email"].subject, "->", s.process(c["email"]))
        store.set_rule("autonomy_level", "1")
        return

    if a.gmail:
        from .google_io import Gmail, SheetMirror
        gmail = Gmail()
        s = Screener(store, ClaudeLLM(), mailer=gmail, own_address=os.environ.get("SCREENER_MAILBOX"))
        sheet = SheetMirror(os.environ["SCREENER_SHEET_ID"], store) if os.environ.get("SCREENER_SHEET_ID") else None
        while True:
            try:
                if sheet:
                    sheet.pull(s)
                for r in store.q("SELECT key FROM settings WHERE key LIKE 'requeue:%'"):
                    gmail.requeue(r["key"].split(":", 1)[1])
                    store.set_setting(r["key"], None)
                if store.enabled():
                    for em in gmail.new_messages():
                        result = s.process(em)
                        logging.info("%s -> %s", em.message_id, result)
                        if result not in ("disabled", "retry"):  # those must be seen again
                            gmail.mark_done(em.message_id)
                    s.tick()
                    if int(time.time()) % 86400 < a.every:
                        s.purge()
                if sheet:
                    sheet.push()
            except Exception:  # noqa: BLE001 - keep polling; the failure is logged and retried next round
                logging.exception("poll failed; retrying next round")
                store.alert("error", "", "Gmail/Sheets poll failed; check the logs if this repeats.")
            time.sleep(a.every)


if __name__ == "__main__":
    main()
