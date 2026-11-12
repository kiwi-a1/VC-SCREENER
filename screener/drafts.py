"""Emails and the memo are filled from fixed templates, not free text from the model.

That keeps every outgoing email free of valuation, terms, money or legal talk at every
autonomy level; `rails.forbidden_topic` double-checks the final text before any send.
"""
from __future__ import annotations

from .models import CRITERIA

SIGN = "\n\nBest regards,\nThe Investment Team\n\n(Pitches sent to this address are first read by an " \
       "automated system. A person makes every decision.)"


def _name(f):
    return f.get("company") or "your company"


def interest(f):
    return (f"Re: {_name(f)}", f"Hi,\n\nThank you for sending {_name(f)} our way. We enjoyed reading about it and "
            "would like to set up a 30-minute call with the founders. Could you share two or three times that "
            "work for you over the next two weeks?" + SIGN)


def request_docs(f, not_read):
    asks = list(f.get("questions_for_founder", []))[:5]
    if not_read:
        asks.insert(0, "We could not open: " + "; ".join(not_read) + ". Could you send these as PDF files?")
    if not asks:
        asks = ["Could you send your pitch deck as a PDF?"]
    lines = "\n".join(f"{i}. {q}" for i, q in enumerate(asks, 1))
    return (f"Re: {_name(f)}", f"Hi,\n\nThank you for reaching out about {_name(f)}. To review it properly we "
            f"need a little more:\n\n{lines}" + SIGN)


def pass_email(f):
    return (f"Re: {_name(f)}", f"Hi,\n\nThank you for thinking of us for {_name(f)}. After a careful look, it is not a "
            "fit for our current focus, so we will not take it further. We wish you the very best with the "
            "raise and appreciate you sharing it." + SIGN)


def ack():
    return ("Re: your pitch", "Hi,\n\nThank you, we have received your pitch and will come back to you." + SIGN)


def memo(f, row, docs_read, not_read) -> str:
    sc = f.get("scores", {})
    table = "\n".join(
        f"| {c.replace('_', ' ')} | {sc.get(c, {}).get('score') if sc.get(c, {}).get('score') is not None else 'could not verify'} "
        f"| {sc.get(c, {}).get('evidence', '')} ({sc.get(c, {}).get('source', '')}) |" for c in CRITERIA)
    flags = "\n".join(f"- **{r['flag']}**: {r['evidence']} ({r['source']})" for r in f.get("red_flags", [])) or "- none found"
    cnv = "\n".join(f"- [could not verify] {c}" for c in f.get("could_not_verify", [])) or "- none"
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(f.get("questions_for_founder", []), 1)) or "none"
    changes = f.get("changes_since_last_pitch") or ""
    return f"""# Screening memo: {_name(f)}
Received {row['date_received']} from {row['sender']} | Score {row['score']}/100 | Confidence {row['confidence']}

## Verdict
**{row['outcome']}**: {f.get('reason', '')}

## Company in brief
{f.get('one_line', '')}
Sector: {f.get('sector', '')} | Stage: {f.get('stage', '')} | Geography: {f.get('geography', '')} | Ask: {f.get('ask', '')} [founder stated]
{('Changes since earlier pitch: ' + changes) if changes else ''}

## Scorecard
| Criterion | Score 0 to 5 | Evidence and page |
|---|---|---|
{table}

## Red flags
{flags}

## Could not verify
{cnv}

## Documents
Read: {', '.join(docs_read) or 'none'}. Not read: {', '.join(not_read) or 'none'}.

## Questions for the founder
{qs}

## GP notes
(Left blank. Written by humans only.)
"""
