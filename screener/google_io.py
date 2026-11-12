"""Google adapters: read the pitch label, write Gmail drafts in-thread, mirror the Sheet.

Scope is deliberately narrow (privacy limit 1): only messages carrying PITCH_LABEL are
listed. Never point this at a partner's personal inbox.

Auth: a service account with domain-wide delegation for the pitch mailbox, or an OAuth
token for that mailbox only. Scopes: gmail.modify (read label + drafts + send), spreadsheets.
"""
from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
import email as email_lib
import email.policy
import json
import os
from email.message import EmailMessage

from .models import PIPELINE_COLUMNS, Attachment, Email
from .pipeline import Mailer
from .store import Store

PITCH_LABEL = os.environ.get("SCREENER_GMAIL_LABEL", "Pitches")
DONE_LABEL = os.environ.get("SCREENER_DONE_LABEL", "Pitches/Screened")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/spreadsheets"]


def credentials():
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    if os.environ.get("GOOGLE_OAUTH_TOKEN_FILE"):
        return Credentials.from_authorized_user_file(os.environ["GOOGLE_OAUTH_TOKEN_FILE"], SCOPES)
    creds = service_account.Credentials.from_service_account_file(os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=SCOPES)
    return creds.with_subject(os.environ["SCREENER_MAILBOX"])


class Gmail(Mailer):
    def __init__(self, creds=None):
        from googleapiclient.discovery import build
        self.svc = build("gmail", "v1", credentials=creds or credentials(), cache_discovery=False)
        labels = self.svc.users().labels().list(userId="me").execute()["labels"]
        by_name = {l["name"]: l["id"] for l in labels}
        self.label_id = by_name[PITCH_LABEL]
        self.done_id = by_name.get(DONE_LABEL) or self.svc.users().labels().create(
            userId="me", body={"name": DONE_LABEL}).execute()["id"]
        self._last_msg: dict[str, str] = {}

    def new_messages(self) -> list[Email]:
        out, token = [], None
        while True:
            resp = self.svc.users().messages().list(userId="me", labelIds=[self.label_id], maxResults=100,
                                                     pageToken=token).execute()
            for m in resp.get("messages", []):
                meta = self.svc.users().messages().get(userId="me", id=m["id"], format="minimal").execute()
                if self.done_id in meta.get("labelIds", []):
                    continue  # already screened; filtered by label id, not by a search string
                raw = self.svc.users().messages().get(userId="me", id=m["id"], format="raw").execute()
                out.append(self._parse(raw))
            token = resp.get("nextPageToken")
            if not token or len(out) >= 50:
                return out

    def mark_done(self, gmail_id: str):
        self.svc.users().messages().modify(userId="me", id=gmail_id, body={"addLabelIds": [self.done_id]}).execute()

    def requeue(self, gmail_id: str):
        """A human chose "screen as pitch" for an unsure email: let the next poll pick it up again."""
        self.svc.users().messages().modify(userId="me", id=gmail_id, body={"removeLabelIds": [self.done_id]}).execute()

    def _parse(self, raw) -> Email:
        msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw["raw"]), policy=email.policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        if body is not None and body.get_content_type() == "text/html":
            text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", text)))
            text = re.sub(r"[ \t]+", " ", text)
        atts = [Attachment(p.get_filename() or "file", p.get_content_type(), p.get_payload(decode=True) or b"")
                for p in msg.iter_attachments()]
        self._last_msg[raw["threadId"]] = msg.get("Message-ID", "")
        received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc).isoformat(timespec="seconds")
        return Email(message_id=raw["id"], thread_id=raw["threadId"], sender=msg.get("From", ""),
                     subject=msg.get("Subject", ""), body=text, attachments=atts, received_at=received)

    def _mime(self, thread_id, to, subject, body):
        m = EmailMessage()
        m["To"], m["Subject"] = to, subject
        if self._last_msg.get(thread_id):
            m["In-Reply-To"] = m["References"] = self._last_msg[thread_id]
        m.set_content(body)
        return {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode(), "threadId": thread_id}

    def save_draft(self, thread_id, to, subject, body) -> str:
        d = self.svc.users().drafts().create(userId="me", body={"message": self._mime(thread_id, to, subject, body)}).execute()
        return d["id"]

    def send(self, thread_id, to, subject, body):
        self.svc.users().messages().send(userId="me", body=self._mime(thread_id, to, subject, body)).execute()

    def send_draft(self, draft_id: str) -> bool:
        from googleapiclient.errors import HttpError
        try:
            self.svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
            return True
        except HttpError as e:
            if e.resp.status == 404:  # the GP deleted the draft: that is a veto
                return False
            raise


class SheetMirror:
    """SQLite is the source of truth; the Sheet is the GP's view.

    push(): rewrites the Pipeline, Inbox log, Lessons and Fund Rules tabs.
    pull(): applies only cells a human CHANGED in the Sheet since the last push (compared with a
    saved snapshot), so an edit made on the dashboard is never overwritten by a stale Sheet value.
    """

    FEEDBACK = ("gp_decision", "agent_right", "agent_right_reason")
    SNAP = "sheet_snapshot"

    def __init__(self, spreadsheet_id: str, store: Store, creds=None):
        from googleapiclient.discovery import build
        self.sid, self.store = spreadsheet_id, store
        self.v = build("sheets", "v4", credentials=creds or credentials(), cache_discovery=False).spreadsheets().values()

    def _snapshot(self) -> dict:
        return json.loads(self.store.get_setting(self.SNAP, "{}") or "{}")

    def pull(self, screener):
        snap = self._snapshot()
        if not snap:
            return  # never pushed yet: nothing in the Sheet can be newer than the store
        rows = self.v.get(spreadsheetId=self.sid, range="Pipeline!A1:ZZ").execute().get("values", [])
        if rows:
            hdr = rows[0]
            for r in rows[1:]:
                rec = dict(zip(hdr, r + [""] * (len(hdr) - len(r))))
                tid = rec.get("thread_id", "")
                was = snap.get("feedback", {}).get(tid)
                now = [rec.get(k, "") for k in self.FEEDBACK]
                cur = self.store.get_row(tid)
                if cur and was is not None and now != was:
                    merged = [n if n != w else cur[k] for n, w, k in zip(now, was, self.FEEDBACK)]
                    screener.record_feedback(tid, *merged)
        fr = self.v.get(spreadsheetId=self.sid, range="Fund Rules!A2:B").execute().get("values", [])
        for k, *v in fr:
            val = v[0] if v else ""
            if k == "autonomy_level" or k not in snap.get("rules", {}):
                continue  # the dial changes only on the dashboard; unknown keys are ignored
            if val != snap["rules"][k]:
                if k == "weights":
                    from .rails import parse_weights
                    try:
                        parse_weights(val)
                    except ValueError as e:
                        self.store.alert("error", "", f"Fund Rules weights in the Sheet were not applied: {e}")
                        continue
                self.store.set_rule(k, val)

    def push(self):
        pipeline = self.store.q("SELECT * FROM pipeline ORDER BY date_received")
        rules = self.store.q("SELECT * FROM fund_rules")
        data = {
            "Pipeline": [PIPELINE_COLUMNS] + [[r[c] for c in PIPELINE_COLUMNS] for r in pipeline],
            "Inbox log": [["date", "sender", "subject", "label", "status"]] + [
                [r["date"], r["sender"], r["subject"], r["label"], r["status"]] for r in self.store.q("SELECT * FROM inbox_log")],
            "Lessons": [["date", "deal", "lesson"]] + [[r["date"], r["thread_id"], r["lesson"]] for r in self.store.q("SELECT * FROM lessons")],
            "Fund Rules": [["setting", "value", "used for"]] + [[r["key"], r["value"], r["used_for"]] for r in rules],
        }
        for tab, values in data.items():
            self.v.clear(spreadsheetId=self.sid, range=f"{tab}!A1:ZZ").execute()
            self.v.update(spreadsheetId=self.sid, range=f"{tab}!A1", valueInputOption="RAW", body={"values": values}).execute()
        self.store.set_setting(self.SNAP, json.dumps({
            "feedback": {r["thread_id"]: [r[k] for k in self.FEEDBACK] for r in pipeline},
            "rules": {r["key"]: r["value"] for r in rules}}))
