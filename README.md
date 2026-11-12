# Pitch Screener

Reads every email in the fund's pitch mailbox, screens it against the Fund Rules, writes
one row per pitch, drafts the reply, and alerts the GP only for what needs them. The GP
opens one dashboard and sees three lists: **strong fits to read**, **things that need
you**, and **everything else**. The GP makes every decision.

## How it works

```
Gmail label "Pitches" ─▶ 1 duplicate check (atomic, both tabs)
                        ├─ known thread? ─▶ resume deal, re-score, same row
                        2 sort (sees sender, subject, file names, first 1,500 chars only)
                        │    not a pitch ─▶ Inbox log (one line, no text)
                        │    unsure / forwarded intro ─▶ Inbox log + alert, a human decides
                        3 read PDF / PPTX / DOCX / public Google links (3 files, 40 pages)
                        4 judge: Claude plans, uses tools, checks (≤12 calls), submits fixed fields
                        5 code: total score, confidence, outcome rules, row, memo if strong
                        6 code: draft (or send, if the autonomy level allows), alert
```

Claude makes only two judgement calls: whether an email is a pitch, and how good the
pitch is. Everything else is code in `screener/rails.py`, and each rule has a test.

## Changes from the spec (from the review)

| Gap | Fix |
|---|---|
| A founder's reply would create a second row | A Thread ID column. A known thread resumes and re-scores the existing row |
| The duplicate check missed Inbox log, and two runs at once could both pass it | An atomic SQLite claim covers both tabs. The Google Sheet is a mirror |
| "Could not verify" had no maths rule | The criterion is left out, the other weights are rescaled to 100, and confidence drops one level for each one left out |
| A two-line Series C email became Needs more | A mismatch the founder states outright goes to Pass, with the quote |
| Workflow tools store email bodies | No workflow tool, and bodies are never logged or stored for non-pitches (tested) |
| Unsure emails had no home | Inbox log with status "awaiting human", plus dashboard buttons |
| Hidden text could be missed | Text is extracted locally and a code scan backs up the model (tested with white-on-white text) |
| Nothing enforced the no-valuation rule in emails | Emails come from fixed templates, and any send mentioning valuation, terms or money is blocked |
| "Matching lessons" was undefined | Same sector first, then the most recent, 10 in all |
| No retention period was set | Fund Rules `retention_days_pass` (365), applied by `Screener.purge()` |
| Tests were missing for the new paths | 16 test pitches: forwarded intro, founder reply, repeat company, broken output, valuation block |

**Your seven tools are now four, plus Claude's built-in web search.** The model gets read
document, fetch page, calculate, look up and web search. The other two, writing to the
Sheet and sending email, became code that runs after the model is done. So the model can
never write a row or send an email itself.

## Fixed in the second review

| Problem | Fix |
|---|---|
| A founder's reply was judged on the new email alone; the original deck was lost | Earlier files are kept per deal and read again |
| Decks were sent as text only, so charts, images and image-only slides were lost | PDFs go to Claude as documents. PPTX and DOCX are converted with LibreOffice first |
| Switching the agent off marked emails done, so they were never screened | Emails that arrive while it is off wait in Gmail |
| A brief API outage turned a pitch into Error for good | Up to 3 retries on later polls, then Error. A crashed run is recovered after 30 minutes |
| An error on a known deal wiped the GP's decision | Error rows merge into the existing row |
| The Sheet sync overwrote dashboard edits with stale Sheet values | Only cells a human changed since the last sync are applied |
| A veto-window pass could go out twice (as a Gmail draft and a separate send) | The same Gmail draft is sent. Deleting it counts as a veto |
| "I wanted to introduce Orbitra" sent normal pitches to a human | Forward detection looks only at structural forward markers |
| The fund's own replies in a thread were screened | Mail from the pitch mailbox is skipped |
| Founder text could steer the questions in an automatic email | Automatic sends are blocked if a question has a link, contact detail or money talk |
| The "$5M" check never matched | Currency check fixed, and words like "million" added |
| The dashboard had no login | A password, or connections from this machine only |
| Gmail dates were not ISO, which broke retention | Gmail's internal timestamp is used |
| HTML-only emails reached the model as raw HTML | HTML is stripped |
| A web page could redirect the agent to an internal address | Every redirect is re-checked, with a 2 MB page cap |
| The 12-call limit changed the tool list mid-run | The tool list is fixed (cache-friendly), and code refuses calls over the limit |
| Level 2 never sent acknowledgements | It now sends one when no other email goes out |

Environment variables: `SCREENER_MAILBOX` (your own address, so its mail is skipped),
`SCREENER_DASHBOARD_PASSWORD`, and `SCREENER_BASE_URL` (so memo links work from the Sheet).

## Run it

```bash
pip install -r requirements.txt
pytest -q tests                       # offline: 44 tests of every rule
export ANTHROPIC_API_KEY=...
python scripts/live_eval.py           # real Claude on 15 of the test pitches (the broken-output case is offline only)
python -m screener.run --demo         # fill a local db with the test pitches
uvicorn screener.web:app              # dashboard at http://localhost:8000
```

**Connect Gmail and the Sheet:**
1. Create a `Pitches` label with a filter that fills it, or a dedicated pitch address.
2. Create a Sheet with the tabs `Pipeline`, `Inbox log`, `Lessons` and `Fund Rules`.
3. Set up Google credentials for **the pitch mailbox only**, with the scopes `gmail.modify` and `spreadsheets`.
4. Set the variables below and start the poller.

```bash
export GOOGLE_SERVICE_ACCOUNT_FILE=sa.json SCREENER_MAILBOX=pitches@yourfund.com SCREENER_SHEET_ID=...
python -m screener.run --gmail
```

Drafts appear as Gmail drafts in the founder's thread, and the GP presses send. The GP can
fill in "GP decision" and "Agent right?" in the Sheet or on the dashboard. Either way, a
"no" or "partly" with a reason becomes a lesson.

**Choosing the models:** the judge uses `claude-sonnet-5-5` and the sort uses
`claude-haiku-5-5`. To change them, set `SCREENER_JUDGE_MODEL` and `SCREENER_SORT_MODEL`.
Judge calls turn on server-side refusal fallback (`fallbacks: "default"`).

**Before going live:** check your Anthropic data-retention terms, and add the founder
notice to the pitch address's signature. The email templates already include it.

## Autonomy dial

The level is set under Fund Rules → `autonomy_level`, and only code reads it. The
dashboard shows how often the GP agreed with the agent.

| Level | What it may send |
|---|---|
| 1 | Nothing |
| 2 | Acknowledgements, document requests and passes for hard mismatches |
| 3 | Also other passes, after a 48-hour veto window. Setting a GP decision vetoes the send |
| 4 | Also interest emails, for scores of 85 or more with high confidence |

At every level, emails about valuation, terms, money or legal matters are blocked.

## Not yet tested against real services

The Google adapters (`screener/google_io.py`) and a live Claude run have not been tested,
because this build had no Google account or API key. The offline tests cover the full
pipeline with a scripted model. Run `scripts/live_eval.py` first.
