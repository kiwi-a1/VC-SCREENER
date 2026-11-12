"""Plain data types shared across the screener."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Attachment:
    name: str
    mime: str
    data: bytes


@dataclass
class Email:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: str = field(default_factory=now_iso)
    attachments: list[Attachment] = field(default_factory=list)

    def head(self, chars: int) -> dict:
        """The only view of an email the sort step is allowed to see."""
        return {
            "sender": self.sender,
            "subject": self.subject,
            "attachment_names": [a.name for a in self.attachments],
            "opening_text": self.body[:chars],
        }


CRITERIA = ["team", "market", "product", "traction", "financials_and_ask"]

OUTCOMES = ["Strong fit", "Needs more", "Pass", "Human review", "Error"]

PIPELINE_COLUMNS = [
    "message_id", "thread_id", "date_received", "company", "sender", "website",
    "sector", "stage", "ask", "score", "confidence", "outcome", "reason",
    "sub_scores", "red_flags", "documents_not_read", "memo_link",
    "draft_status", "autonomy_level", "gp_decision", "agent_right",
    "agent_right_reason", "updated_at",
]
