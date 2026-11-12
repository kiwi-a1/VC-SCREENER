"""The six-step loop. Fixed code at the edges, the model in the middle."""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import drafts, rails
from .llm import JudgeContext
from .models import Attachment, Email, now_iso
from .reader import read_email
from .store import Store

log = logging.getLogger("screener")  # never logs email bodies

MAX_ATTEMPTS = 3          # transient failures (API down, rate limit) are retried on later polls
STALE_AFTER_MIN = 30      # a claim stuck in "processing" this long means a run crashed


class Mailer:
    """Where emails go. The base class keeps everything local (the dashboard is the outbox);
    google_io.Gmail writes real Gmail drafts in the founder's thread."""

    def save_draft(self, thread_id, to, subject, body) -> str:
        return ""

    def send(self, thread_id, to, subject, body) -> None:
        log.info("local mode: email to %s recorded in the dashboard, not delivered", to)

    def send_draft(self, draft_id: str) -> bool:
        """Send a saved draft. False if the draft no longer exists (a human deleted it = veto)."""
        return True


def is_transient(e: Exception) -> bool:
    try:
        import anthropic
        if isinstance(e, (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)):
            return True
        if isinstance(e, anthropic.APIStatusError) and e.status_code in (408, 409, 429, 529) or \
                isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
            return True
    except ImportError:
        pass
    return isinstance(e, (TimeoutError, ConnectionError))


class Screener:
    def __init__(self, store: Store, llm, mailer: Mailer | None = None, deals_dir="deals", http=None,
                 convert_to_pdf: bool = True, own_address: str | None = None):
        self.store, self.llm, self.mailer = store, llm, mailer or Mailer()
        self.deals_dir, self.http, self.convert = Path(deals_dir), http, convert_to_pdf
        self.own = (own_address or os.environ.get("SCREENER_MAILBOX", "")).lower()

    # ------------------------------------------------------------- entry point
    def process(self, email: Email) -> str:
        """Returns the outcome, or one of: disabled, duplicate, retry, not_pitch, unsure, own_mail."""
        if not self.store.enabled():
            return "disabled"  # the caller must NOT mark the email done; it is processed when switched on
        # Step 1: duplicate check. Atomic claim over BOTH tabs, before anything else.
        if not self.store.claim(email.message_id, email.thread_id):
            return "duplicate"
        flag = f"force_pitch:{email.message_id}"
        force_pitch = self.store.get_setting(flag) is not None  # a human chose "screen as pitch"
        try:
            if self.own and self.own in email.sender.lower():
                # Our own sent replies can land in the thread; never screen the fund's own mail.
                self.store.log_inbox(email.message_id, email.thread_id, email.received_at, email.sender,
                                     email.subject, "own_mail", "logged")
                self.store.set_claim_tab(email.message_id, "inbox_log")
                return "own_mail"
            prior = self.store.get_row(email.thread_id)
            if prior:  # a founder reply on a known deal: resume it, no new row
                self.store.set_claim_tab(email.message_id, "pipeline")
                return self._screen(email, prior=prior)
            if not force_pitch:
                label = self._sort(email)
                if label != "pitch":
                    return label
            self.store.set_claim_tab(email.message_id, "pipeline")
            result = self._screen(email)
            self.store.set_setting(flag, None)
            return result
        except Exception as e:  # noqa: BLE001 - rule 5: never drop a pitch silently
            if is_transient(e) and self.store.attempts(email.message_id) < MAX_ATTEMPTS:
                log.warning("transient failure on %s, will retry: %s", email.message_id, type(e).__name__)
                self.store.release_for_retry(email.message_id)
                return "retry"
            log.exception("processing failed for %s", email.message_id)
            self._error_row(email, f"{type(e).__name__}: {e}")
            return "Error"

    # ------------------------------------------------------------- step 2
    def _sort(self, email: Email) -> str:
        head = email.head(rails.SORT_CHARS)
        if rails.looks_forwarded(email.subject, head["opening_text"]):
            label = "unsure"  # forwarded intros: a human decides whether to read more
        else:
            label = self.llm.sort(head).get("label", "unsure")
            if label not in ("pitch", "not_pitch", "unsure"):
                label = "unsure"
        if label == "pitch":
            return label
        status = "awaiting human" if label == "unsure" else "logged"
        self.store.log_inbox(email.message_id, email.thread_id, email.received_at, email.sender,
                             email.subject, label, status)
        self.store.set_claim_tab(email.message_id, "inbox_log")
        if label == "unsure":
            self.store.alert("unsure", email.thread_id,
                             f"Unsure email from {email.sender}: '{email.subject}'. Open the dashboard to decide.")
        return label

    # ------------------------------------------------------------- steps 3-6
    def _screen(self, email: Email, prior: dict | None = None) -> str:
        rules = self.store.rules()
        weights = rails.parse_weights(rules["weights"])
        level = rails.autonomy_level(rules)

        # Step 3: read. On a founder reply, the files from earlier emails are read again too.
        earlier = self._kept_files(email.thread_id) if prior else []
        new_names = {Path(a.name).name for a in email.attachments}
        earlier = [a for a in earlier if a.name not in new_names]
        rr = read_email(email, self.http, earlier=earlier, convert=self.convert)
        self._keep_files(email)
        all_text = email.body + "\n".join(d.as_text() for d in rr.docs)
        injection_hits = rails.scan_for_injection(all_text)

        def lookup(company, sector):
            return {"earlier_rows": [self._row_summary(r) for r in self.store.rows_by_company(company, email.thread_id)],
                    "lessons": [l["lesson"] for l in self.store.matching_lessons(sector)]}

        same_domain = self.store.rows_by_sender_domain(email.sender, email.thread_id)
        ctx = JudgeContext(
            fund_rules_text=self._rules_text(rules), email_text=email.body, sender=email.sender,
            subject=email.subject, docs={d.name: d.pages for d in rr.docs}, documents_not_read=rr.not_read,
            links=rr.links, lessons=[l["lesson"] for l in self.store.matching_lessons((prior or {}).get("sector", ""))],
            previous_rows=[self._row_summary(r) for r in same_domain], lookup=lookup,
            prior_assessment=self.store.get_assessment(email.thread_id) if prior else None,
            pdfs={d.name: d.pdf for d in rr.docs if d.pdf})

        # Step 4: judge (one retry on broken output)
        f, err = None, ""
        for _ in range(2):
            res = self.llm.judge(ctx)
            if res.fields is not None:
                errs = rails.validate_fields(res.fields)
                if not errs:
                    f = res.fields
                    break
                err = "; ".join(errs)
            else:
                err = res.error
        if f is None:
            self._error_row(email, f"judge output invalid after retry: {err}")
            return "Error"

        earlier_rows = {r["thread_id"]: r for r in
                        self.store.rows_by_company(f["company"], email.thread_id) + same_domain}

        if injection_hits and not f["suspicious_instruction"]["found"]:
            f["suspicious_instruction"] = {"found": True, "quote": injection_hits[0]}

        score, unverified = rails.total_score(f["scores"], weights)
        f["confidence"] = confidence = rails.adjust_confidence(f["confidence"], len(unverified))
        # "Nothing could be read": no document opened, and the email itself is too short to judge.
        material = rr.anything_read or (len(email.body.strip()) >= 200 and not rr.not_read)
        outcome = rails.decide_outcome(f, score, rules, material)

        # Step 5: record
        row = dict(prior or {})
        row.update({
            "message_id": email.message_id, "thread_id": email.thread_id,
            "date_received": row.get("date_received") or email.received_at,
            "company": f["company"], "sender": row.get("sender") or email.sender, "website": f.get("website", ""),
            "sector": f["sector"], "stage": f["stage"], "ask": f["ask"],
            "score": "" if score is None else score, "confidence": confidence, "outcome": outcome,
            "reason": f["reason"] + (f" (pitched before: {len(earlier_rows)} earlier row(s))" if earlier_rows else ""),
            "sub_scores": {c: f["scores"][c]["score"] for c in f["scores"]},
            "red_flags": [r["flag"] for r in f["red_flags"]],
            "documents_not_read": rr.not_read, "autonomy_level": level,
        })
        if outcome == "Strong fit" or row.get("memo_link"):
            row["memo_link"] = self.store.save_memo(
                email.thread_id, drafts.memo(f, row, [d.name for d in rr.docs], rr.not_read))
        self.store.save_assessment(email.thread_id, f)

        # Step 6: act
        row["draft_status"] = self._act(email, f, outcome, score, level, rr.not_read, is_new=prior is None)
        self.store.upsert_row(row)
        if outcome == "Strong fit":
            self.store.alert("strong_fit", email.thread_id,
                             f"Strong fit: {f['company']} scored {score}/100. Memo: {row['memo_link']}")
        if outcome == "Human review":
            self.store.alert("human_review", email.thread_id,
                             f"Suspicious instruction in {f['company']} pitch: \"{f['suspicious_instruction']['quote'][:200]}\"")
        return outcome

    def _act(self, email, f, outcome, score, level, not_read, is_new) -> str:
        if outcome == "Human review":
            return "no draft (human review)"
        if outcome == "Strong fit":
            kind, (subj, body) = "interest", drafts.interest(f)
        elif outcome == "Pass":
            kind, (subj, body) = "pass", drafts.pass_email(f)
        else:
            kind, (subj, body) = "request_docs", drafts.request_docs(f, not_read)
        send_now, after_veto = rails.may_send(kind, level, f, outcome, score)
        if send_now or after_veto:
            bad = rails.forbidden_topic(body) or (rails.unsafe_model_text(f["questions_for_founder"])
                                                 if kind == "request_docs" else None)
            if bad:
                send_now = after_veto = False
                self.store.alert("blocked_send", email.thread_id, f"Not sent automatically ({bad}); kept as a draft.")

        status = ""
        if send_now:
            self.mailer.send(email.thread_id, email.sender, subj, body)
            self.store.add_draft(email.thread_id, email.sender, subj, body, kind, "sent")
            status = f"{kind} sent (level {level})"
        elif after_veto:
            when = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(timespec="seconds")
            ext = self.mailer.save_draft(email.thread_id, email.sender, subj, body)
            self.store.add_draft(email.thread_id, email.sender, subj, body, kind, "scheduled", send_after=when,
                                 external_id=ext)
            status = f"{kind} scheduled after 48h veto window (delete the draft or set a GP decision to stop it)"
        else:
            ext = self.mailer.save_draft(email.thread_id, email.sender, subj, body)
            self.store.add_draft(email.thread_id, email.sender, subj, body, kind, "draft", external_id=ext)
            status = f"{kind} drafted"

        # Level 2+: if nothing goes out now, tell a new founder their pitch arrived.
        if is_new and not send_now and rails.may_send("ack", level, f, outcome, score)[0]:
            a_subj, a_body = drafts.ack()
            self.mailer.send(email.thread_id, email.sender, a_subj, a_body)
            self.store.add_draft(email.thread_id, email.sender, a_subj, a_body, "ack", "sent")
            status += "; acknowledgement sent"
        return status

    # ------------------------------------------------------------- helpers
    def _error_row(self, email: Email, why: str):
        row = self.store.get_row(email.thread_id) or {}  # never wipe an existing deal's data
        row.update({"message_id": email.message_id, "thread_id": email.thread_id,
                    "date_received": row.get("date_received") or email.received_at,
                    "sender": row.get("sender") or email.sender, "company": row.get("company") or email.subject[:80],
                    "outcome": "Error",
                    "reason": why[:300] + (f" (previous outcome: {row['outcome']})" if row.get("outcome") else "")})
        self.store.upsert_row(row)
        self.store.set_claim_tab(email.message_id, "pipeline")
        self.store.alert("error", email.thread_id, f"Screening failed for '{email.subject}': {why[:200]}")

    def _keep_files(self, email: Email):
        if not email.attachments:
            return
        d = self.deals_dir / email.thread_id
        d.mkdir(parents=True, exist_ok=True)
        for a in email.attachments:
            (d / Path(a.name).name).write_bytes(a.data)

    def _kept_files(self, thread_id: str) -> list[Attachment]:
        d = self.deals_dir / thread_id
        return [Attachment(p.name, "", p.read_bytes()) for p in sorted(d.iterdir())] if d.is_dir() else []

    @staticmethod
    def _row_summary(r: dict) -> dict:
        keep = ("date_received", "company", "sector", "stage", "ask", "score", "outcome", "reason", "gp_decision")
        return {k: r.get(k, "") for k in keep}

    @staticmethod
    def _rules_text(rules: dict) -> str:
        skip = {"autonomy_level", "gp_alert_contact", "retention_days_pass"}  # code-only settings
        return "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in rules.items() if k not in skip and v)

    # ------------------------------------------------------------- scheduled jobs
    def tick(self):
        """Run every poll: send veto-window drafts that are due, and recover crashed runs."""
        now = now_iso()
        for d in self.store.q("SELECT * FROM drafts WHERE status='scheduled' AND send_after<=?", (now,)):
            row = self.store.get_row(d["thread_id"]) or {}
            if row.get("gp_decision") or rails.autonomy_level(self.store.rules()) < 3 or not self.store.enabled():
                self.store.x("UPDATE drafts SET status='draft', note='vetoed: GP decision, level change or switched off' "
                             "WHERE id=?", (d["id"],))
                continue
            if d["external_id"]:
                sent = self.mailer.send_draft(d["external_id"])  # sending the same draft: never a duplicate email
            else:
                self.mailer.send(d["thread_id"], d["to_addr"], d["subject"], d["body"])
                sent = True
            self.store.x("UPDATE drafts SET status=? WHERE id=?", ("sent" if sent else "vetoed (draft deleted)", d["id"]))

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_AFTER_MIN)).isoformat(timespec="seconds")
        for c in self.store.stale_claims(cutoff):
            self.store.release_for_retry(c["message_id"])
            self.store.alert("error", c["thread_id"], f"A run stopped mid-way on message {c['message_id']}; "
                                                      "it will be retried on the next poll.")

    def purge(self):
        """Retention: delete deal folders of Pass rows older than the Fund Rules period."""
        days = int(self.store.rules().get("retention_days_pass", "365"))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        for r in self.store.q("SELECT thread_id FROM pipeline WHERE outcome='Pass' AND date_received<?", (cutoff,)):
            shutil.rmtree(self.deals_dir / r["thread_id"], ignore_errors=True)

    # ------------------------------------------------------------- GP feedback -> lessons
    def record_feedback(self, thread_id: str, gp_decision: str, agent_right: str, reason: str):
        row = self.store.get_row(thread_id)
        if not row:
            raise KeyError(thread_id)
        new_lesson = agent_right in ("no", "partly") and reason.strip() and reason.strip() != row["agent_right_reason"]
        self.store.update_row(thread_id, gp_decision=gp_decision, agent_right=agent_right, agent_right_reason=reason)
        if new_lesson:
            self.store.add_lesson(thread_id, row["sector"], reason.strip())

    def autonomy_report(self) -> dict:
        """Evidence for the dial. Code reports; only the GP changes the level."""
        marked = self.store.q("SELECT agent_right FROM pipeline WHERE agent_right IN ('yes','no','partly')")
        agree = sum(1 for r in marked if r["agent_right"] == "yes")
        return {"level": rails.autonomy_level(self.store.rules()), "decisions_marked": len(marked),
                "agreement": round(100 * agree / len(marked), 1) if marked else None,
                "ready_for_level_2": len(marked) >= 30 and agree / max(1, len(marked)) >= 0.9}
