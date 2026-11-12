"""Run the test pitches through REAL Claude and check the pass marks.

  ANTHROPIC_API_KEY=... python scripts/live_eval.py

Costs real money (roughly 16 judge runs). Re-run after any change to a prompt,
the Fund Rules or the code.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures.pitches import cases  # noqa: E402
from screener.llm import ClaudeLLM  # noqa: E402
from screener.pipeline import Screener  # noqa: E402
from screener.store import Store  # noqa: E402

d = Path(tempfile.mkdtemp())
store = Store(d / "live.db")
s = Screener(store, ClaudeLLM(), deals_dir=d / "deals")
fails = []
for c in cases():
    if c["fake"] == "BROKEN":
        continue  # the broken-output path is covered offline
    store.set_rule("autonomy_level", str(c["expect"].get("level", 1)))
    got = s.process(c["email"])
    row = store.get_row(c["email"].thread_id) or {}
    exp, problems = c["expect"], []
    if "outcome" in exp and got != exp["outcome"]:
        problems.append(f"outcome {got} != {exp['outcome']}")
    if "outcome_in" in exp and got not in exp["outcome_in"]:
        problems.append(f"outcome {got} not in {exp['outcome_in']}")
    for flag in exp.get("red_flags", []) + ([exp["red_flag_contains"]] if "red_flag_contains" in exp else []):
        if flag not in row.get("red_flags", "").lower():
            problems.append(f"red flag '{flag}' not found")
    if exp.get("result") and got != exp["result"]:
        problems.append(f"result {got}")
    print(f"{c['id']:>2} {'OK ' if not problems else 'FAIL'} {c['email'].subject[:45]:45} -> {got}  {'; '.join(problems)}")
    if problems:
        fails.append(c["id"])

in_thesis_passed = store.q("SELECT company FROM pipeline WHERE outcome='Pass' AND lower(sector) LIKE '%space%' "
                           "AND lower(stage) IN ('seed','series a')")
if in_thesis_passed:
    fails.append("in-thesis pitch marked Pass: " + ", ".join(r["company"] for r in in_thesis_passed))
print("\nALL PASS" if not fails else f"\nFAILED: {fails}")
sys.exit(1 if fails else 0)
