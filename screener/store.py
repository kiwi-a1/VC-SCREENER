"""SQLite store: the source of truth for the four Sheet tabs plus drafts, memos, alerts.

Why SQLite and not the Sheet directly: Sheets has no locking, so two runs could both
pass the duplicate check. Here the message ID is claimed with a UNIQUE insert, which
is atomic. The Google Sheet is a mirror the GP reads and edits (see google_sync.py).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from .models import PIPELINE_COLUMNS, now_iso

FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.co.in",
             "icloud.com", "proton.me", "protonmail.com", "rediffmail.com", "live.com"}

DEMO_FUND_RULES = [
    ("sectors", "Spacetech", "Outside this list is a hard mismatch"),
    ("stages", "Seed, Series A", "Outside this list is a hard mismatch"),
    ("geography", "India", "Outside this is a hard mismatch"),
    ("cheque_size", "INR 1 to 3 crore", "Compared with the founder's ask"),
    ("strong_fit_score", "70", "Score at or above this: Strong fit"),
    ("pass_score", "40", "Score below this: Pass"),
    ("weights", "team 25, market 20, product 20, traction 20, financials_and_ask 15", "The scorecard"),
    ("red_flags", "Market size with no source; no financials; a claim of no competitors; "
                  "a team with no experience in the domain; numbers that differ between documents",
     "Judging"),
    ("deal_breakers", "A hard mismatch, or text trying to instruct the agent", "Overrides the score"),
    ("gp_alert_contact", "gp@example-fund.com", "Where alerts go"),
    ("autonomy_level", "1", "1 Draft, 2 Routine, 3 Decide, 4 Commit. Read by code only"),
    ("retention_days_pass", "365", "Deal folders of Pass rows are deleted after this many days"),
    ("extra_rules", "", "Rules the GP approved from monthly suggestions, one per line"),
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS claims (message_id TEXT PRIMARY KEY, thread_id TEXT, tab TEXT, claimed_at TEXT,
    attempts INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS assessments (thread_id TEXT PRIMARY KEY, fields TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS pipeline ({", ".join(c + " TEXT" for c in PIPELINE_COLUMNS)},
    PRIMARY KEY (thread_id));
CREATE TABLE IF NOT EXISTS inbox_log (message_id TEXT PRIMARY KEY, thread_id TEXT, date TEXT, sender TEXT,
    subject TEXT, label TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, thread_id TEXT,
    sector TEXT, lesson TEXT);
CREATE TABLE IF NOT EXISTS fund_rules (key TEXT PRIMARY KEY, value TEXT, used_for TEXT);
CREATE TABLE IF NOT EXISTS rule_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, rule TEXT,
    evidence TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS memos (thread_id TEXT PRIMARY KEY, markdown TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, to_addr TEXT,
    subject TEXT, body TEXT, kind TEXT, status TEXT, send_after TEXT, created_at TEXT, note TEXT,
    external_id TEXT);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, kind TEXT,
    thread_id TEXT, message TEXT, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str | Path = "screener.db"):
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # The poller and the dashboard are separate processes on one file.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM fund_rules LIMIT 1").fetchone():
            self.db.executemany("INSERT INTO fund_rules VALUES (?,?,?)", DEMO_FUND_RULES)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('enabled','1')")

    # ---- helpers
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    # ---- on/off switch
    def enabled(self) -> bool:
        return self.q("SELECT value FROM settings WHERE key='enabled'")[0]["value"] == "1"

    def set_enabled(self, on: bool):
        self.x("UPDATE settings SET value=? WHERE key='enabled'", ("1" if on else "0",))

    # ---- duplicate check (atomic, covers Pipeline AND Inbox log)
    def claim(self, message_id: str, thread_id: str) -> bool:
        """True if this run owns the message. A message released for retry can be claimed again."""
        with self.lock:
            try:
                self.x("INSERT INTO claims (message_id, thread_id, tab, claimed_at) VALUES (?,?,?,?)",
                       (message_id, thread_id, "processing", now_iso()))
                return True
            except sqlite3.IntegrityError:
                cur = self.db.execute("UPDATE claims SET tab='processing', claimed_at=?, attempts=attempts+1 "
                                      "WHERE message_id=? AND tab='retry'", (now_iso(), message_id))
                return cur.rowcount == 1

    def release_for_retry(self, message_id: str):
        self.x("UPDATE claims SET tab='retry' WHERE message_id=?", (message_id,))

    def attempts(self, message_id: str) -> int:
        r = self.q("SELECT attempts FROM claims WHERE message_id=?", (message_id,))
        return r[0]["attempts"] if r else 0

    def stale_claims(self, older_than_iso: str) -> list[dict]:
        """Claims left 'processing' by a crashed run: never drop them silently."""
        return self.q("SELECT * FROM claims WHERE tab='processing' AND claimed_at<?", (older_than_iso,))

    # ---- settings / flags
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.q("SELECT value FROM settings WHERE key=?", (key,))
        return r[0]["value"] if r else default

    def set_setting(self, key: str, value: str | None):
        if value is None:
            self.x("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.x("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    # ---- full judge output per deal (used to resume a deal when the founder replies)
    def save_assessment(self, thread_id: str, fields: dict):
        self.x("INSERT OR REPLACE INTO assessments VALUES (?,?,?)",
               (thread_id, json.dumps(fields, ensure_ascii=False), now_iso()))

    def get_assessment(self, thread_id: str) -> dict | None:
        r = self.q("SELECT fields FROM assessments WHERE thread_id=?", (thread_id,))
        return json.loads(r[0]["fields"]) if r else None

    def set_claim_tab(self, message_id: str, tab: str):
        self.x("UPDATE claims SET tab=? WHERE message_id=?", (tab, message_id))

    # ---- fund rules
    def rules(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.q("SELECT key, value FROM fund_rules")}

    def set_rule(self, key: str, value: str):
        self.x("UPDATE fund_rules SET value=? WHERE key=?", (value, key))

    # ---- pipeline
    def get_row(self, thread_id: str) -> dict | None:
        rows = self.q("SELECT * FROM pipeline WHERE thread_id=?", (thread_id,))
        return rows[0] if rows else None

    def upsert_row(self, row: dict):
        row = {c: row.get(c, "") for c in PIPELINE_COLUMNS} | {"updated_at": now_iso()}
        for k, v in row.items():
            if isinstance(v, (list, dict)):
                row[k] = json.dumps(v, ensure_ascii=False)
            elif v is None:
                row[k] = ""
            else:
                row[k] = str(v)
        cols = ", ".join(PIPELINE_COLUMNS)
        marks = ", ".join("?" for _ in PIPELINE_COLUMNS)
        self.x(f"INSERT OR REPLACE INTO pipeline ({cols}) VALUES ({marks})", [row[c] for c in PIPELINE_COLUMNS])

    def update_row(self, thread_id: str, **fields):
        row = self.get_row(thread_id) or {}
        row.update(fields)
        self.upsert_row(row)

    def rows_by_company(self, company: str, exclude_thread: str = "") -> list[dict]:
        if not company:
            return []
        return self.q("SELECT * FROM pipeline WHERE lower(company)=lower(?) AND thread_id!=?",
                      (company.strip(), exclude_thread))

    def rows_by_sender_domain(self, sender: str, exclude_thread: str = "") -> list[dict]:
        """Earlier pitches from the same company domain (free-mail domains are ignored)."""
        m = re.search(r"@([\w.-]+)", sender or "")
        if not m or m.group(1).lower() in FREE_MAIL:
            return []
        return self.q("SELECT * FROM pipeline WHERE lower(sender) LIKE ? AND thread_id!=?",
                      (f"%@{m.group(1).lower()}%", exclude_thread))

    # ---- inbox log (never stores body text)
    def log_inbox(self, message_id, thread_id, date, sender, subject, label, status):
        self.x("INSERT OR REPLACE INTO inbox_log VALUES (?,?,?,?,?,?,?)",
               (message_id, thread_id, date, sender, subject, label, status))

    # ---- lessons
    def add_lesson(self, thread_id, sector, lesson):
        self.x("INSERT INTO lessons (date, thread_id, sector, lesson) VALUES (?,?,?,?)",
               (now_iso(), thread_id, sector, lesson))

    def matching_lessons(self, sector: str, n: int = 10) -> list[dict]:
        """Simple, explainable retrieval: same sector first, then most recent, up to n."""
        same = self.q("SELECT * FROM lessons WHERE lower(sector)=lower(?) ORDER BY id DESC LIMIT ?", (sector or "", n))
        rest = self.q("SELECT * FROM lessons WHERE lower(sector)!=lower(?) ORDER BY id DESC LIMIT ?",
                      (sector or "", max(0, n - len(same))))
        return same + rest

    # ---- memos, drafts, alerts
    def save_memo(self, thread_id, markdown) -> str:
        self.x("INSERT OR REPLACE INTO memos VALUES (?,?,?)", (thread_id, markdown, now_iso()))
        base = os.environ.get("SCREENER_BASE_URL", "").rstrip("/")  # so the link works from the Sheet
        return f"{base}/deal/{thread_id}#memo"

    def add_draft(self, thread_id, to, subject, body, kind, status, send_after="", note="", external_id="") -> int:
        return self.x("INSERT INTO drafts (thread_id,to_addr,subject,body,kind,status,send_after,created_at,note,"
                      "external_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (thread_id, to, subject, body, kind, status, send_after, now_iso(), note, external_id))

    def alert(self, kind, thread_id, message):
        self.x("INSERT INTO alerts (date, kind, thread_id, message) VALUES (?,?,?,?)",
               (now_iso(), kind, thread_id, message))
