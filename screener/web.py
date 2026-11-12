"""GP dashboard. One page that answers: what needs me today?

Run:  uvicorn screener.web:app --reload
"""
from __future__ import annotations

import html
import json
import os
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse

from .pipeline import Screener
from .store import Store

store = Store(os.environ.get("SCREENER_DB", "screener.db"))
PASSWORD = os.environ.get("SCREENER_DASHBOARD_PASSWORD", "")


def require_login(credentials: HTTPBasicCredentials = Depends(HTTPBasic(auto_error=False))):
    """The dashboard can switch the agent off and change what it may send, so it needs a password.
    Without SCREENER_DASHBOARD_PASSWORD it only accepts connections from this machine."""
    if PASSWORD:
        if not credentials or not secrets.compare_digest(credentials.password, PASSWORD):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Pitch Screener", dependencies=[Depends(require_login)])


@app.middleware("http")
async def local_only_without_password(request: Request, call_next):
    if not PASSWORD and request.client and request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return HTMLResponse("Set SCREENER_DASHBOARD_PASSWORD to open the dashboard from another machine.", 403)
    return await call_next(request)
_screener: Screener | None = None


def screener() -> Screener:
    global _screener
    if _screener is None:
        from .llm import ClaudeLLM
        _screener = Screener(store, ClaudeLLM())
    return _screener


e = lambda s: html.escape(str(s if s is not None else ""))  # noqa: E731

CSS = """<style>
:root{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--card:#fff;--line:#e7e5e4;--good:#15803d;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--mut:#a8a29e;--card:#292524;--line:#44403c}}
body{font:15px/1.5 system-ui;background:var(--bg);color:var(--fg);max-width:1100px;margin:0 auto;padding:16px}
a{color:inherit}table{width:100%;border-collapse:collapse;background:var(--card)}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.o-Strong{color:var(--good);font-weight:600}.o-Pass{color:var(--mut)}.o-Human,.o-Error{color:var(--bad);font-weight:600}
.o-Needs{color:var(--warn)}small,.mut{color:var(--mut)}pre{white-space:pre-wrap;background:var(--card);padding:12px;border:1px solid var(--line);border-radius:8px}
button{padding:4px 10px}
</style>"""


def page(title, body):
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'><title>{e(title)}</title>"
                        f"{CSS}<h1><a href='/'>Pitch Screener</a></h1>{body}")


def oclass(o):
    return "o-" + (o or "").split(" ")[0]


@app.get("/", response_class=HTMLResponse)
def home():
    rules = store.rules()
    on = store.enabled()
    alerts = store.q("SELECT * FROM alerts WHERE seen=0 ORDER BY id DESC")
    strong = store.q("SELECT * FROM pipeline WHERE outcome='Strong fit' AND gp_decision='' ORDER BY score+0 DESC")
    needs_me = store.q("SELECT * FROM pipeline WHERE outcome IN ('Human review','Error') AND gp_decision=''")
    unsure = store.q("SELECT * FROM inbox_log WHERE status='awaiting human'")
    pending = store.q("SELECT count(*) n FROM drafts WHERE status IN ('draft','scheduled')")[0]["n"]
    rows = store.q("SELECT * FROM pipeline ORDER BY date_received DESC LIMIT 200")
    rep = screener_report()

    def deal_rows(rs):
        return "".join(
            f"<tr><td><a href='/deal/{e(r['thread_id'])}'>{e(r['company'])}</a><br><small>{e(r['sender'])}</small></td>"
            f"<td>{e(r['score'])}</td><td>{e(r['confidence'])}</td><td class='{oclass(r['outcome'])}'>{e(r['outcome'])}</td>"
            f"<td>{e(r['reason'])}</td><td><small>{e(r['draft_status'])}</small></td><td>{e(r['gp_decision'])}</td></tr>"
            for r in rs) or "<tr><td colspan=7 class=mut>Nothing here.</td></tr>"

    head = "<tr><th>Company</th><th>Score</th><th>Conf.</th><th>Outcome</th><th>Reason</th><th>Draft</th><th>GP</th></tr>"
    body = f"""
<div class=card>Agent is <b>{'ON' if on else 'OFF'}</b>
 <form method=post action=/toggle style=display:inline><button>{'Switch off' if on else 'Switch on'}</button></form>
 &nbsp; Autonomy level <b>{rails_level(rules)}</b> &nbsp; <small>{e(rep)}</small>
 &nbsp; Drafts waiting for you: <a href=/drafts><b>{pending}</b></a> &nbsp; <a href=/rules>Fund Rules</a> · <a href=/inbox>Inbox log</a> · <a href=/lessons>Lessons</a></div>
<h2>Strong fits to read ({len(strong)})</h2><table>{head}{deal_rows(strong)}</table>
<h2>Needs you ({len(needs_me) + len(unsure)})</h2><table>{head}{deal_rows(needs_me)}</table>
{''.join(f"<div class=card>Unsure email from {e(u['sender'])}: <b>{e(u['subject'])}</b> <form method=post action=/unsure/{e(u['message_id'])} style=display:inline><button name=d value=pitch>Screen as pitch</button> <button name=d value=not_pitch>Not a pitch</button></form><br><small>Its text was not stored; choosing 'screen as pitch' makes the poller read it again from Gmail.</small></div>" for u in unsure)}
<h2>Alerts</h2>{''.join(f"<div class=card><small>{e(a['date'])} · {e(a['kind'])}</small><br>{e(a['message'])}</div>" for a in alerts[:20]) or '<p class=mut>None.</p>'}
<form method=post action=/alerts/seen><button>Mark alerts read</button></form>
<h2>All pitches</h2><table>{head}{deal_rows(rows)}</table>"""
    return page("Pitch Screener", body)


def rails_level(rules):
    from .rails import autonomy_level
    return autonomy_level(rules)


def screener_report():
    marked = store.q("SELECT agent_right FROM pipeline WHERE agent_right IN ('yes','no','partly')")
    if not marked:
        return "No GP feedback yet: mark 'Agent right?' on deals to earn the next level."
    agree = sum(r["agent_right"] == "yes" for r in marked)
    return f"GP agreed on {agree}/{len(marked)} ({round(100 * agree / len(marked))}%). Level 2 needs 30 marked at 90%+."


@app.get("/deal/{thread_id}", response_class=HTMLResponse)
def deal(thread_id: str):
    r = store.get_row(thread_id)
    if not r:
        return page("Not found", "<p>No such deal.</p>")
    memo = store.q("SELECT markdown FROM memos WHERE thread_id=?", (thread_id,))
    fields = store.get_assessment(thread_id)
    ds = store.q("SELECT * FROM drafts WHERE thread_id=? ORDER BY id DESC", (thread_id,))
    body = f"""<h2>{e(r['company'])} <span class='{oclass(r['outcome'])}'>{e(r['outcome'])}</span></h2>
<p>{e(r['reason'])}</p><p>Score <b>{e(r['score'])}</b>/100 · confidence {e(r['confidence'])} · {e(r['sector'])} · {e(r['stage'])} · ask {e(r['ask'])}</p>
<p class=mut>Red flags: {e(', '.join(json.loads(r['red_flags'] or '[]')) or 'none')} · Documents not read: {e(', '.join(json.loads(r['documents_not_read'] or '[]')) or 'none')}</p>
<div class=card><form method=post action=/deal/{e(thread_id)}/feedback>
<b>Your decision</b> <select name=gp_decision><option value=''>-</option>{''.join(f"<option {'selected' if r['gp_decision']==v else ''}>{v}</option>" for v in ('meet','pass','ask for more'))}</select>
 &nbsp; <b>Agent right?</b> <select name=agent_right><option value=''>-</option>{''.join(f"<option {'selected' if r['agent_right']==v else ''}>{v}</option>" for v in ('yes','no','partly'))}</select>
 <input name=reason placeholder='If no/partly: why, in one sentence (becomes a lesson)' size=60 value='{e(r['agent_right_reason'])}'>
 <button>Save</button></form></div>
<h3 id=memo>Memo</h3>{f"<pre>{e(memo[0]['markdown'])}</pre>" if memo else '<p class=mut>Memos are created for strong fits only.</p>'}
<h3>Drafts</h3>{''.join(f"<div class=card><small>{e(d['kind'])} · {e(d['status'])} {e(d['send_after'])}</small><br><b>{e(d['subject'])}</b><pre>{e(d['body'])}</pre></div>" for d in ds) or '<p class=mut>None.</p>'}
<details><summary>Full assessment (JSON)</summary><pre>{e(json.dumps(fields, indent=2, ensure_ascii=False)) if fields else ''}</pre></details>"""
    return page(r["company"], body)


@app.post("/deal/{thread_id}/feedback")
def feedback(thread_id: str, gp_decision: str = Form(""), agent_right: str = Form(""), reason: str = Form("")):
    Screener(store, llm=None).record_feedback(thread_id, gp_decision, agent_right, reason)
    return RedirectResponse(f"/deal/{thread_id}", 303)


@app.post("/toggle")
def toggle():
    store.set_enabled(not store.enabled())
    return RedirectResponse("/", 303)


@app.post("/alerts/seen")
def seen():
    store.x("UPDATE alerts SET seen=1")
    return RedirectResponse("/", 303)


@app.post("/unsure/{message_id}")
def unsure(message_id: str, d: str = Form(...)):
    store.x("UPDATE inbox_log SET status=?, label=? WHERE message_id=?",
            ("human: screen as pitch" if d == "pitch" else "human: not a pitch", d, message_id))
    if d == "pitch":
        store.release_for_retry(message_id)                     # allow one re-run...
        store.set_setting(f"force_pitch:{message_id}", "1")     # ...that skips the sort
        store.set_setting(f"requeue:{message_id}", "1")         # Gmail mode: the poller re-reads it
    return RedirectResponse("/", 303)


@app.get("/drafts", response_class=HTMLResponse)
def drafts_page():
    ds = store.q("SELECT d.*, p.company FROM drafts d LEFT JOIN pipeline p USING(thread_id) "
                 "WHERE d.status IN ('draft','scheduled') ORDER BY d.id DESC")
    return page("Drafts", "<h2>Drafts waiting for a human</h2><p class=mut>In Gmail mode these are real Gmail drafts in "
                "the thread; press send there.</p>" + "".join(
                    f"<div class=card><a href='/deal/{e(d['thread_id'])}'>{e(d['company'])}</a> · {e(d['kind'])} · {e(d['status'])}"
                    f"<br>To {e(d['to_addr'])}: <b>{e(d['subject'])}</b><pre>{e(d['body'])}</pre></div>" for d in ds))


@app.get("/rules", response_class=HTMLResponse)
def rules_page():
    rs = store.q("SELECT * FROM fund_rules")
    props = store.q("SELECT * FROM rule_proposals WHERE status='proposed'")
    return page("Fund Rules", "<h2>Fund Rules</h2><p class=mut>Edit to change the fund's focus. No code changes.</p>"
                "<form method=post action=/rules><table>" + "".join(
                    f"<tr><td>{e(r['key'])}<br><small>{e(r['used_for'])}</small></td><td><textarea name='{e(r['key'])}' rows=2 cols=70>{e(r['value'])}</textarea></td></tr>"
                    for r in rs) + "</table><button>Save</button></form><h3>Suggested rule changes</h3>" + "".join(
                    f"<div class=card>{e(p['rule'])}<br><small>{e(p['evidence'])}</small><form method=post action=/proposal/{p['id']}>"
                    f"<button name=d value=approve>Approve</button> <button name=d value=reject>Reject</button></form></div>" for p in props)
                + "<form method=post action=/proposals/generate><button>Suggest changes from GP feedback</button></form>")


@app.post("/rules")
async def save_rules(request: Request):
    form = await request.form()
    from .rails import parse_weights
    known = store.rules()
    try:
        if "weights" in form:
            parse_weights(form["weights"])  # refuse a broken scorecard
        for k in ("strong_fit_score", "pass_score", "retention_days_pass"):
            if k in form:
                float(form[k])
        if "autonomy_level" in form and form["autonomy_level"] not in ("1", "2", "3", "4"):
            raise ValueError("autonomy level must be 1, 2, 3 or 4")
    except ValueError as err:
        return page("Not saved", f"<p>Not saved: {e(err)}</p><p><a href=/rules>Back</a></p>")
    for k, v in form.items():
        if k in known:
            store.set_rule(k, v)
    return RedirectResponse("/rules", 303)


@app.post("/proposals/generate")
def gen_proposals():
    from .llm import LessonRuleLLM
    rows = store.q("SELECT company, sector, stage, score, outcome, reason, gp_decision, agent_right, agent_right_reason FROM pipeline")
    for p in LessonRuleLLM().propose(rows, store.q("SELECT * FROM lessons"), Screener._rules_text(store.rules())):
        store.x("INSERT INTO rule_proposals (date, rule, evidence, status) VALUES (datetime('now'),?,?,'proposed')",
                (p["rule"], p["evidence"]))
    return RedirectResponse("/rules", 303)


@app.post("/proposal/{pid}")
def decide_proposal(pid: int, d: str = Form(...)):
    p = store.q("SELECT * FROM rule_proposals WHERE id=?", (pid,))[0]
    if d == "approve":
        extra = store.rules().get("extra_rules", "")
        store.set_rule("extra_rules", (extra + "\n" + p["rule"]).strip())
    store.x("UPDATE rule_proposals SET status=? WHERE id=?", ("approved" if d == "approve" else "rejected", pid))
    return RedirectResponse("/rules", 303)


@app.get("/inbox", response_class=HTMLResponse)
def inbox():
    rs = store.q("SELECT * FROM inbox_log ORDER BY date DESC LIMIT 300")
    return page("Inbox log", "<h2>Inbox log</h2><p class=mut>One line per non-pitch. No email text is stored.</p><table>"
                "<tr><th>Date</th><th>Sender</th><th>Subject</th><th>Label</th><th>Status</th></tr>" + "".join(
                    f"<tr><td>{e(r['date'])}</td><td>{e(r['sender'])}</td><td>{e(r['subject'])}</td><td>{e(r['label'])}</td><td>{e(r['status'])}</td></tr>" for r in rs) + "</table>")


@app.get("/lessons", response_class=HTMLResponse)
def lessons():
    rs = store.q("SELECT * FROM lessons ORDER BY id DESC")
    return page("Lessons", "<h2>Lessons</h2><table><tr><th>Date</th><th>Deal</th><th>Lesson</th></tr>" + "".join(
        f"<tr><td>{e(r['date'])}</td><td><a href='/deal/{e(r['thread_id'])}'>{e(r['thread_id'])}</a></td><td>{e(r['lesson'])}</td></tr>" for r in rs) + "</table>")
