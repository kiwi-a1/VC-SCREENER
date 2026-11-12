"""The rails that never move. Plain code, no model. Every rule here has a unit test."""
from __future__ import annotations

import ast
import operator
import re

from .models import CRITERIA

TOOL_CALL_LIMIT = 12
SORT_CHARS = 1500
MAX_FILES = 3
MAX_PAGES = 40
READABLE_EXT = {".pdf", ".pptx", ".docx"}


# ---------------------------------------------------------------- fund rules parsing
def parse_weights(text: str) -> dict[str, int]:
    weights = {}
    for name, num in re.findall(r"([a-z_ ]+?)\s+(\d+)", text.lower()):
        weights[name.strip().replace(" ", "_")] = int(num)
    missing = set(CRITERIA) - set(weights)
    if missing or sum(weights[c] for c in CRITERIA) != 100:
        raise ValueError(f"Fund Rules weights must cover {CRITERIA} and add to 100, got {weights}")
    return {c: weights[c] for c in CRITERIA}


def autonomy_level(rules: dict) -> int:
    try:
        level = int(rules.get("autonomy_level", "1"))
    except ValueError:
        return 1
    return level if level in (1, 2, 3, 4) else 1


# ---------------------------------------------------------------- scoring
CONF_ORDER = ["low", "medium", "high"]


def total_score(scores: dict, weights: dict[str, int]) -> tuple[float | None, list[str]]:
    """Return (score out of 100, unverified criteria).

    A criterion marked could-not-verify (score None) is LEFT OUT and the remaining
    weights are rescaled to 100, so a thin deck is not silently scored as 0. Each one
    left out costs one confidence level (applied by `adjust_confidence`).
    """
    used, unverified = {}, []
    for c in CRITERIA:
        s = scores.get(c, {}).get("score")
        if s is None:
            unverified.append(c)
        else:
            used[c] = max(0, min(5, int(s)))
    if not used:
        return None, unverified
    wsum = sum(weights[c] for c in used)
    total = sum(used[c] / 5 * weights[c] for c in used) * 100 / wsum
    return round(total, 1), unverified


def adjust_confidence(conf: str, n_unverified: int) -> str:
    conf = conf if conf in CONF_ORDER else "low"
    return CONF_ORDER[max(0, CONF_ORDER.index(conf) - n_unverified)]


# ---------------------------------------------------------------- outcome rules
def decide_outcome(f: dict, score: float | None, rules: dict, anything_read: bool) -> str:
    """Checked in order, first match wins.

    1 suspicious instruction            -> Human review
    2 hard mismatch stated explicitly   -> Pass   (fix: a clear "we're raising our Series C"
                                                   must not become Needs more just because
                                                   the email is short; a Pass is only a draft)
    3 low confidence or nothing read    -> Needs more
    4 hard mismatch (inferred)          -> Pass
    5 score >= strong fit               -> Strong fit
    6 score <  pass                     -> Pass
    7 else                              -> Needs more
    """
    strong, low = float(rules["strong_fit_score"]), float(rules["pass_score"])
    mm = f.get("hard_mismatch", {})
    if f.get("suspicious_instruction", {}).get("found"):
        return "Human review"
    if mm.get("is_mismatch") and mm.get("explicit") and mm.get("quote"):
        return "Pass"
    if f.get("confidence") == "low" or not anything_read or score is None:
        return "Needs more"
    if mm.get("is_mismatch"):
        return "Pass"
    if score >= strong:
        return "Strong fit"
    if score < low:
        return "Pass"
    return "Needs more"


# ---------------------------------------------------------------- injection backup scan
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|prompts?)",
    r"\b(rate|score|mark|grade)\s+(this|us|the deck|it)\b.{0,30}\b(10|100|five|5)\s*(/|out of)\s*(10|100|5)",
    r"\byou are (an? )?(ai|assistant|language model|llm|screening (agent|bot))",
    r"\b(system prompt|developer message)\b",
    r"\b(as an ai|dear (ai|agent|bot|assistant))\b",
    r"\b(mark|classify|label) (this|it) as (a )?(strong fit|pass|pitch)",
]


def scan_for_injection(text: str) -> list[str]:
    hits = []
    low = text.lower()
    for p in INJECTION_PATTERNS:
        m = re.search(p, low)
        if m:
            hits.append(text[max(0, m.start() - 40): m.end() + 40].strip())
    return hits


# ---------------------------------------------------------------- forwarded intros
# Structural markers only. Phrases like "I wanted to introduce Orbitra" are written by
# founders pitching directly, so they are left to the model, whose prompt sends real
# forwarded intros to "unsure".
FORWARD_BODY = re.compile(r"(-{3,}\s*forwarded message\s*-{3,}|begin forwarded message:|^from:.*\n.*^(sent|date):)",
                          re.I | re.M)
FORWARD_SUBJECT = re.compile(r"^\s*(fwd?|fw)\s*:|\bintro\b.*(<>|<->|/|\+)|(<>|<->)", re.I)


def looks_forwarded(subject: str, opening: str) -> bool:
    return bool(FORWARD_SUBJECT.search(subject or "") or FORWARD_BODY.search(opening[:800]))


# ---------------------------------------------------------------- send permissions
FORBIDDEN_TOPICS = re.compile(
    r"\b(valuation|valued|pre-?money|post-?money|term ?sheets?|terms of (the )?(deal|investment|round)|"
    r"safe note|convertible|equity|dilution|cap ?table|ownership|stake|cheque|check size|wire|legal|"
    r"lawyer|nda|liquidation|board seat|crore|lakh|million|billion|usd|inr|rupees?|dollars?)\b"
    r"|[$₹€£]\s?\d", re.I)
CONTACT_OR_LINK = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{8,}\d")


def forbidden_topic(text: str) -> str | None:
    m = FORBIDDEN_TOPICS.search(text)
    return m.group(0) if m else None


def unsafe_model_text(questions: list[str]) -> str | None:
    """Model-written lines that would go into an automatic email. Founder material can steer the
    model, so before any automatic send these must be short and carry no links, addresses or
    numbers that look like contact details."""
    for q in questions:
        if len(q) > 250:
            return "question longer than 250 characters"
        if CONTACT_OR_LINK.search(q):
            return f"link or contact detail in: {q[:80]}"
    return None


def may_send(kind: str, level: int, f: dict, outcome: str, score: float | None) -> tuple[bool, bool]:
    """Return (send_now, send_after_veto_window). Level 1 never sends."""
    hard = f.get("hard_mismatch", {}).get("is_mismatch", False)
    if level >= 2 and kind in ("ack", "request_docs") and outcome != "Human review":
        return True, False
    if level >= 2 and kind == "pass" and hard:
        return True, False
    if level >= 3 and kind == "pass":
        return False, True
    if level >= 4 and kind == "interest" and (score or 0) >= 85 and f.get("confidence") == "high":
        return True, False
    return False, False


# ---------------------------------------------------------------- calculator (no eval)
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}


def calculate(expr: str) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            if isinstance(n.op, ast.Pow) and abs(ev(n.right)) > 100:
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only numbers and + - * / % ** ( ) are allowed")
    if len(expr) > 300:
        raise ValueError("expression too long")
    return ev(ast.parse(expr.replace(",", ""), mode="eval"))


# ---------------------------------------------------------------- judge output validation
def validate_fields(f: dict) -> list[str]:
    errs = []
    for k in ("company", "one_line", "sector", "stage", "ask", "scores", "red_flags", "could_not_verify",
              "hard_mismatch", "confidence", "suspicious_instruction", "questions_for_founder", "reason"):
        if k not in f:
            errs.append(f"missing {k}")
    if errs:
        return errs
    for c in CRITERIA:
        s = f["scores"].get(c)
        if not isinstance(s, dict) or "score" not in s:
            errs.append(f"scores.{c} missing")
            continue
        if s["score"] is not None and not (isinstance(s["score"], int) and 0 <= s["score"] <= 5):
            errs.append(f"scores.{c}.score must be 0-5 or null")
        if s["score"] is not None and not s.get("evidence"):
            errs.append(f"scores.{c} has a score but no evidence (rule 3)")
    if f["confidence"] not in CONF_ORDER:
        errs.append("confidence must be high/medium/low")
    if len(f["questions_for_founder"]) > 5:
        errs.append("at most 5 questions")
    return errs
