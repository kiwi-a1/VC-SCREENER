"""Offline end-to-end tests of the six steps and every rail, with a scripted fake model.

These prove the CODE behaves (duplicates, outcomes, privacy, sends, errors). Whether the
real model finds the planted red flags is checked by scripts/live_eval.py.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures.pitches import cases  # noqa: E402
from screener import rails  # noqa: E402
from screener.llm import JudgeResult  # noqa: E402
from screener.pipeline import Mailer, Screener  # noqa: E402
from screener.store import Store  # noqa: E402

CASES = {c["id"]: c for c in cases()}


class FakeLLM:
    def __init__(self):
        self.sort_inputs = []
        self.contexts = []
        self.current = None
        self.raise_next = None

    def sort(self, head):
        self.sort_inputs.append(head)
        return {"label": self.current["fake_sort"], "why": "fake"}

    def judge(self, ctx):
        self.contexts.append(ctx)
        if self.raise_next:
            e, self.raise_next = self.raise_next, None
            raise e
        f = self.current["fake"]
        if f == "BROKEN":
            return JudgeResult({"company": "x"}, 1)
        return JudgeResult(json.loads(json.dumps(f)), 3)


class RecordingMailer(Mailer):
    def __init__(self):
        self.sent, self.drafts = [], []

    def save_draft(self, t, to, s, b):
        self.drafts.append((t, s, b))
        return f"gmail-draft-{len(self.drafts)}"

    def send(self, t, to, s, b):
        self.sent.append((t, s, b))

    def send_draft(self, draft_id):
        self.sent.append(("draft", draft_id, ""))
        return draft_id != "deleted"


def locked(_req):
    return httpx.Response(403, text="sign in")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("s")
    store = Store(d / "t.db")
    llm, mailer = FakeLLM(), RecordingMailer()
    s = Screener(store, llm, mailer, deals_dir=d / "deals",
                 http=httpx.Client(transport=httpx.MockTransport(locked)), convert_to_pdf=False)
    results = {}
    for cid in sorted(CASES):
        c = CASES[cid]
        llm.current = c
        store.set_rule("autonomy_level", str(c["expect"].get("level", 1)))
        results[cid] = s.process(c["email"])
    store.set_rule("autonomy_level", "1")
    return store, llm, mailer, results, s


def row(store, cid):
    return store.get_row(CASES[cid]["email"].thread_id)


def alerts(store, kind):
    return store.q("SELECT * FROM alerts WHERE kind=?", (kind,))


@pytest.mark.parametrize("cid", [1, 2, 3, 4, 5, 8, 9, 15])
def test_outcomes(run, cid):
    store, *_ , results, _ = run
    exp = CASES[cid]["expect"]["outcome"]
    # case 2 was later updated by the founder reply (13); check the result returned at the time
    assert results[cid] == exp, (cid, results[cid], row(store, cid))


def test_strong_fit_memo_alert_and_interest_draft(run):
    store, *_ = run
    r = row(store, 1)
    assert r["memo_link"] and "Screening memo: Orbitra" in store.q("SELECT markdown FROM memos WHERE thread_id='thr-1'")[0]["markdown"]
    assert any("Orbitra" in a["message"] for a in alerts(store, "strong_fit"))
    assert store.q("SELECT kind FROM drafts WHERE thread_id='thr-1'")[0]["kind"] == "interest"


def test_scores_out_of_100_computed_by_code(run):
    store, *_ = run
    assert float(row(store, 1)["score"]) == 81.0


def test_unverified_criteria_rescale_and_lower_confidence(run):
    store, _, _, results, _ = run
    # NovaGrid first read: financials unverified -> medium drops to low -> Needs more
    assert results[2] == "Needs more"


def test_explicit_mismatch_passes_even_when_thin(run):
    store, *_ = run
    assert row(store, 4)["outcome"] == "Pass" and row(store, 4)["confidence"] == "low"


def test_contradiction_and_planted_flags_recorded(run):
    store, *_ = run
    assert "differ" in row(store, 6)["red_flags"]
    flags = row(store, 7)["red_flags"].lower()
    assert "no source" in flags and "no competitors" in flags


def test_hidden_instruction_caught_by_code_even_if_model_misses_it(run):
    store, *_ = run
    r = row(store, 8)
    assert r["outcome"] == "Human review"
    assert not store.q("SELECT 1 FROM drafts WHERE thread_id='thr-8'")
    assert alerts(store, "human_review")


def test_unreadable_formats_listed_and_pdf_requested(run):
    store, *_ = run
    nr = row(store, 9)["documents_not_read"]
    assert "GroundNet.key" in nr and "LOCKED123" in nr
    body = store.q("SELECT body FROM drafts WHERE thread_id='thr-9'")[0]["body"]
    assert "PDF" in body


def test_newsletter_logged_without_text(run):
    store, *_ = run
    assert row(store, 10) is None
    log = store.q("SELECT * FROM inbox_log WHERE message_id='msg-10'")
    assert len(log) == 1 and log[0]["label"] == "not_pitch"
    dump = "\n".join(str(r) for t in ("inbox_log", "pipeline", "settings", "alerts", "drafts") for r in store.q(f"SELECT * FROM {t}"))
    assert "Unsubscribe here" not in dump


def test_sort_sees_only_the_head(run):
    _, llm, *_ = run
    for h in llm.sort_inputs:
        assert set(h) == {"sender", "subject", "attachment_names", "opening_text"}
        assert len(h["opening_text"]) <= rails.SORT_CHARS


def test_duplicate_makes_no_second_row(run):
    store, _, _, results, _ = run
    assert results[11] == "duplicate"
    assert len(store.q("SELECT * FROM pipeline WHERE lower(company)='orbitra'")) == 2  # thr-1 and the new thr-14


def test_forwarded_intro_goes_to_human(run):
    store, *_ = run
    assert store.q("SELECT status FROM inbox_log WHERE message_id='msg-12'")[0]["status"] == "awaiting human"
    assert alerts(store, "unsure") and row(store, 12) is None


def test_founder_reply_updates_same_row(run):
    store, _, _, results, _ = run
    assert results[13] == "Strong fit"
    assert len(store.q("SELECT * FROM pipeline WHERE thread_id='thr-2'")) == 1
    assert row(store, 2)["outcome"] == "Strong fit"


def test_repeat_company_noted(run):
    store, *_ = run
    assert "pitched before" in row(store, 14)["reason"]


def test_broken_output_becomes_error_row(run):
    store, *_ = run
    assert row(store, 15)["outcome"] == "Error" and alerts(store, "error")


def test_valuation_never_sent(run):
    store, _, mailer, _, _ = run
    assert "drafted" in row(store, 16)["draft_status"]
    assert alerts(store, "blocked_send")
    assert not any("valuation" in b.lower() for *_, b in mailer.sent)


def test_level_1_never_sends(run):
    _, _, mailer, _, _ = run
    # Cases 1-15 ran at level 1: nothing sent. Case 16 (level 2): the document request was blocked
    # because it mentions valuation, so only the acknowledgement went out.
    assert [m[1] for m in mailer.sent] == ["Re: your pitch"]


def test_every_pitch_has_a_row_with_reason(run):
    store, *_ = run
    for r in store.q("SELECT * FROM pipeline"):
        assert r["outcome"] and r["reason"]


def test_feedback_becomes_lesson_and_dial_report(run):
    store, _, _, _, s = run
    s.record_feedback("thr-3", "pass", "no", "Payments infra for satellite operators counts as spacetech.")
    assert store.matching_lessons("Fintech")[0]["lesson"].startswith("Payments infra")
    assert s.autonomy_report()["decisions_marked"] == 1


def test_switch_off_stops_everything(tmp_path):
    store = Store(tmp_path / "x.db")
    store.set_enabled(False)
    assert Screener(store, FakeLLM()).process(CASES[1]["email"]) == "disabled"


# ---------------------------------------------------------------- pure rail unit tests
def test_weights_must_add_to_100():
    with pytest.raises(ValueError):
        rails.parse_weights("team 30, market 20, product 20, traction 20, financials and ask 15")


def test_outcome_order():
    rules = {"strong_fit_score": "70", "pass_score": "40"}
    base = {"confidence": "high", "hard_mismatch": {}, "suspicious_instruction": {}}
    assert rails.decide_outcome(base, 90, rules, True) == "Strong fit"
    assert rails.decide_outcome(base | {"confidence": "low"}, 20, rules, True) == "Needs more"  # thin never Pass
    assert rails.decide_outcome(base, 30, rules, True) == "Pass"
    assert rails.decide_outcome(base, 55, rules, True) == "Needs more"
    assert rails.decide_outcome(base, 90, rules, False) == "Needs more"
    assert rails.decide_outcome(base | {"suspicious_instruction": {"found": True}}, 90, rules, True) == "Human review"


def test_calculator_is_safe():
    assert rails.calculate("1,200,000 * 12 / 100") == 144000
    with pytest.raises(ValueError):
        rails.calculate("__import__('os').system('ls')")


def test_send_permissions():
    f = {"hard_mismatch": {"is_mismatch": False}, "confidence": "high"}
    assert rails.may_send("pass", 1, f, "Pass", 20) == (False, False)
    assert rails.may_send("pass", 2, {"hard_mismatch": {"is_mismatch": True}}, "Pass", 20) == (True, False)
    assert rails.may_send("pass", 3, f, "Pass", 20) == (False, True)
    assert rails.may_send("interest", 3, f, "Strong fit", 90) == (False, False)
    assert rails.may_send("interest", 4, f, "Strong fit", 90) == (True, False)
    assert rails.may_send("interest", 4, f, "Strong fit", 80) == (False, False)


# ---------------------------------------------------------------- fixes from the second review
def make(tmp_path, **kw):
    store = Store(tmp_path / "x.db")
    llm, mailer = FakeLLM(), RecordingMailer()
    s = Screener(store, llm, mailer, deals_dir=tmp_path / "deals", convert_to_pdf=False,
                 http=httpx.Client(transport=httpx.MockTransport(locked)), **kw)
    return store, llm, mailer, s


def test_founder_reply_rereads_earlier_files(run):
    _, llm, *_ = run
    ctx = [c for c in llm.contexts if c.subject.startswith("Re: Launchpad")][0]
    assert {"NovaGrid.pptx", "NovaGrid_financials.pdf"} <= set(ctx.docs)
    assert ctx.prior_assessment["company"] == "NovaGrid"


def test_switched_off_email_is_not_lost(tmp_path):
    store, llm, _, s = make(tmp_path)
    llm.current = CASES[3]
    store.set_enabled(False)
    assert s.process(CASES[3]["email"]) == "disabled"
    store.set_enabled(True)
    assert s.process(CASES[3]["email"]) == "Pass"  # not "duplicate"


def test_transient_failure_retries_then_succeeds(tmp_path):
    store, llm, _, s = make(tmp_path)
    llm.current = CASES[3]
    llm.raise_next = ConnectionError("API unreachable")
    assert s.process(CASES[3]["email"]) == "retry"
    assert store.get_row("thr-3") is None
    assert s.process(CASES[3]["email"]) == "Pass"


def test_error_on_known_deal_keeps_gp_data(tmp_path):
    store, llm, _, s = make(tmp_path)
    llm.current = CASES[2]
    s.process(CASES[2]["email"])
    s.record_feedback("thr-2", "ask for more", "yes", "")
    llm.current = dict(CASES[13], fake="BROKEN")
    assert s.process(CASES[13]["email"]) == "Error"
    r = store.get_row("thr-2")
    assert r["gp_decision"] == "ask for more" and r["company"] == "NovaGrid" and "previous outcome" in r["reason"]


def test_own_replies_are_not_screened(tmp_path):
    store, llm, _, s = make(tmp_path, own_address="pitches@fund.in")
    from screener.models import Email
    em = Email("m-own", "thr-1", "Fund <pitches@fund.in>", "Re: Orbitra", "Thanks, let's meet.")
    assert s.process(em) == "own_mail" and store.get_row("thr-1") is None


def test_unsure_email_can_be_screened_after_human_says_pitch(tmp_path):
    store, llm, _, s = make(tmp_path)
    c = CASES[12]
    llm.current = dict(c, fake=CASES[1]["fake"])
    assert s.process(c["email"]) == "unsure"
    # the dashboard button does exactly this:
    store.release_for_retry(c["email"].message_id)
    store.set_setting(f"force_pitch:{c['email'].message_id}", "1")
    assert s.process(c["email"]) == "Needs more"  # body only, no deck: thin, never a Strong fit
    assert store.get_row(c["email"].thread_id)


def test_veto_window(tmp_path):
    store, llm, mailer, s = make(tmp_path)
    store.set_rule("autonomy_level", "3")
    from fixtures.pitches import assess
    llm.current = dict(CASES[3], fake=assess("Weak", scores=(1, 1, 1, 1, 1)))  # in thesis, low score
    assert s.process(CASES[3]["email"]) == "Pass"
    d = store.q("SELECT * FROM drafts WHERE kind='pass'")[0]
    assert d["status"] == "scheduled" and d["external_id"]
    s.tick()
    assert store.q("SELECT status FROM drafts WHERE id=?", (d["id"],))[0]["status"] == "scheduled"  # not due yet
    store.x("UPDATE drafts SET send_after='2000-01-01' WHERE id=?", (d["id"],))
    s.tick()
    assert store.q("SELECT status FROM drafts WHERE id=?", (d["id"],))[0]["status"] == "sent"
    assert ("draft", d["external_id"], "") in mailer.sent  # the same Gmail draft is sent: no duplicate email


def test_veto_by_gp_decision(tmp_path):
    store, llm, mailer, s = make(tmp_path)
    store.set_rule("autonomy_level", "3")
    from fixtures.pitches import assess
    llm.current = dict(CASES[3], fake=assess("Weak", scores=(1, 1, 1, 1, 1)))
    s.process(CASES[3]["email"])
    s.record_feedback("thr-3", "meet", "no", "Weak deck but great founder.")
    store.x("UPDATE drafts SET send_after='2000-01-01'")
    s.tick()
    assert store.q("SELECT status FROM drafts WHERE kind='pass'")[0]["status"] == "draft"


def test_stale_claim_is_recovered(tmp_path):
    store, llm, _, s = make(tmp_path)
    store.claim("m-crash", "thr-x")
    store.x("UPDATE claims SET claimed_at='2000-01-01' WHERE message_id='m-crash'")
    s.tick()
    assert store.claim("m-crash", "thr-x") and store.q("SELECT * FROM alerts WHERE kind='error'")


def test_forwarded_detection_is_structural():
    assert rails.looks_forwarded("Fwd: intro", "")
    assert rails.looks_forwarded("Intro: Kalpa <> Fund", "")
    assert rails.looks_forwarded("Kalpa", "---------- Forwarded message ---------\nFrom: x")
    assert not rails.looks_forwarded("Orbitra seed", "Hi, I wanted to introduce Orbitra, our refuelling startup.")


def test_outgoing_text_guards():
    assert rails.forbidden_topic("We can offer $5M") and rails.forbidden_topic("expected pre-money?")
    assert rails.forbidden_topic(drafts_text()) is None
    assert rails.unsafe_model_text(["Please email cfo@x.com"]) and rails.unsafe_model_text(["See https://x.y"])
    assert rails.unsafe_model_text(["What is your monthly burn?"]) is None


def drafts_text():
    from screener import drafts
    f = {"company": "Orbitra", "questions_for_founder": ["What is your burn?"]}
    return "\n".join([drafts.interest(f)[1], drafts.pass_email(f)[1], drafts.request_docs(f, [])[1], drafts.ack()[1]])


def test_sheet_sync_never_overwrites_dashboard_edits(tmp_path):
    from screener.google_io import SheetMirror
    store, llm, _, s = make(tmp_path)
    llm.current = CASES[3]
    s.process(CASES[3]["email"])

    class Values:
        def __init__(self):
            self.tabs = {}

        def _r(self, v):
            return type("R", (), {"execute": lambda _self: v})()

        def clear(self, spreadsheetId, range):
            self.tabs[range.split("!")[0]] = []
            return self._r({})

        def update(self, spreadsheetId, range, valueInputOption, body):
            self.tabs[range.split("!")[0]] = body["values"]
            return self._r({})

        def get(self, spreadsheetId, range):
            tab = range.split("!")[0]
            vals = self.tabs.get(tab, [])
            return self._r({"values": vals[1:] if range.endswith("A2:B") else vals})

    m = SheetMirror.__new__(SheetMirror)
    m.sid, m.store, m.v = "sid", store, Values()
    m.pull(s)          # before the first push: nothing applied
    m.push()
    s.record_feedback("thr-3", "pass", "yes", "")      # GP uses the dashboard...
    m.pull(s)                                          # ...the stale Sheet must not undo it
    assert store.get_row("thr-3")["gp_decision"] == "pass"
    m.push()
    hdr = m.v.tabs["Pipeline"][0]
    m.v.tabs["Pipeline"][1][hdr.index("agent_right")] = "no"   # GP edits the Sheet
    m.v.tabs["Pipeline"][1][hdr.index("agent_right_reason")] = "Fintech for satellites counts."
    m.pull(s)
    r = store.get_row("thr-3")
    assert r["agent_right"] == "no" and r["gp_decision"] == "pass"
    assert store.q("SELECT * FROM lessons")


def test_pptx_and_docx_convert_to_pdf_for_claude():
    import shutil
    if not shutil.which("soffice"):
        pytest.skip("LibreOffice not installed")
    from screener.reader import read_email
    r = read_email(CASES[6]["email"])
    assert all(d.pdf and d.pdf.startswith(b"%PDF") for d in r.docs)
