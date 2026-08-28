"""Validate Genie Intelligence Groundedness — 3 Golden Test Scenarios.

Tests whether the Genie Space reasons through the intelligence layers
rather than reading raw snapshot flags (threshold alerter behavior).

3 Test Cases:
  1. ANOMALY TEST  — Genie must flag anomaly BEFORE recommending a restock.
  2. VETO TEST     — Genie must recognize open POs cover the gap (no new order).
  3. TRANSFER TEST — Genie must surface lateral transfer (Option A) before PO (Option B).

Usage:
    python3 scripts/validate_genie_groundedness.py --profile anurag-r
"""

import argparse
import textwrap
import datetime

from databricks.sdk import WorkspaceClient

GENIE_SPACE_ID = "01f19b9fca901478a0a4808eebc9437b"

# ─────────────────────────────────────────────────────────────
# Golden Test Cases
# Each maps to one intelligence nuance.
# ─────────────────────────────────────────────────────────────

GOLDEN_TESTS = [
    {
        "id": "T1_ANOMALY",
        "name": "Anomaly Detection (Layer 1 — Forecast & Signal Validation)",
        "question": (
            "Check part P1003 at warehouse WH003. "
            "It shows stockout risk in the snapshot, but what is its consumption anomaly score "
            "and average daily consumption? Should we raise a restock quote or is there an anomaly?"
        ),
        "pass_criteria": [
            "Calls consumption_anomaly_score or avg_daily_consumption",
            "Evaluates burn rate or anomaly score before making recommendation",
        ],
        "fail_criteria": [
            "Recommends ordering without checking consumption or anomaly score",
        ],
    },
    {
        "id": "T2_VETO",
        "name": "Restock Veto — Open PO Coverage (Layer 2 — Procurement Intelligence)",
        "question": (
            "Part P1003 at warehouse WH003 has low stock compared to safety stock. "
            "Check whether existing open procurement orders (pending procurement quantity) "
            "cover the shortfall before recommending a new purchase order."
        ),
        "pass_criteria": [
            "Calls pending_procurement_qty or requested_restock_qty",
            "Compares open PO pending qty against requested restock qty",
            "Resolves whether a new PO is needed or partially/fully covered",
        ],
        "fail_criteria": [
            "Recommends a new PO without checking open procurement pending quantity",
        ],
    },
    {
        "id": "T3_TRANSFER",
        "name": "Lateral Transfer Priority (Layer 2 — Network Surplus before PO)",
        "question": (
            "For part P1003 at warehouse WH003, check if any other warehouse in the network "
            "has surplus stock (via network_surplus) that can be laterally transferred before raising a new PO. "
            "Present internal transfer as Option A and external PO as Option B."
        ),
        "pass_criteria": [
            "Calls network_surplus function",
            "Evaluates internal warehouse transfer options",
        ],
        "fail_criteria": [
            "Recommends supplier PO without checking network surplus",
        ],
    },
]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def extract_response_text(msg) -> str:
    """Extract readable text from a GenieMessage."""
    parts = []
    if hasattr(msg, "attachments") and msg.attachments:
        for att in msg.attachments:
            if hasattr(att, "text") and att.text:
                parts.append(att.text.content or "")
            if hasattr(att, "query") and att.query:
                parts.append(f"[SQL executed: {att.query.query or ''}]")
    if not parts and hasattr(msg, "content") and msg.content:
        parts.append(msg.content)
    return "\n".join(parts).strip() or "<no text response>"


def evaluate_response(response_text: str, test: dict) -> dict:
    """Simple keyword-based groundedness evaluation."""
    text_lower = response_text.lower()
    passed = []
    failed_pass = []
    triggered_fails = []

    for criterion in test["pass_criteria"]:
        keywords = criterion.lower().split()
        # Check if most keywords appear in the response
        matches = sum(1 for kw in keywords if len(kw) > 4 and kw in text_lower)
        if matches >= max(1, len([k for k in keywords if len(k) > 4]) // 2):
            passed.append(f"  ✅ {criterion}")
        else:
            failed_pass.append(f"  ❌ NOT MET: {criterion}")

    for criterion in test["fail_criteria"]:
        keywords = criterion.lower().split()
        matches = sum(1 for kw in keywords if len(kw) > 4 and kw in text_lower)
        if matches >= max(1, len([k for k in keywords if len(k) > 4]) // 2):
            triggered_fails.append(f"  🚨 TRIGGERED: {criterion}")

    is_grounded = len(failed_pass) == 0 and len(triggered_fails) == 0
    return {
        "grounded": is_grounded,
        "passed": passed,
        "failed_pass": failed_pass,
        "triggered_fails": triggered_fails,
    }


def separator(char="─", width=72):
    return char * width


def run_test(w: WorkspaceClient, test: dict, index: int, total: int) -> bool:
    print(f"\n{separator('═')}")
    print(f"  TEST {index}/{total}: {test['name']}")
    print(f"  ID: {test['id']}")
    print(separator())
    print("\n📨 Question sent to Genie:\n")
    print(textwrap.indent(textwrap.fill(test["question"], width=68), "  "))
    print(f"\n⏳ Waiting for Genie response...")

    try:
        msg = w.genie.start_conversation_and_wait(
            space_id=GENIE_SPACE_ID,
            content=test["question"],
            timeout=datetime.timedelta(seconds=120),
        )
        response_text = extract_response_text(msg)
    except Exception as e:
        print(f"\n🔴 ERROR: Genie call failed — {e}")
        return False

    print("\n🤖 Genie Response:\n")
    print(textwrap.indent(response_text[:2000], "  "))
    if len(response_text) > 2000:
        print(f"  ... [truncated — full length: {len(response_text)} chars]")

    # Evaluate groundedness
    result = evaluate_response(response_text, test)

    print(f"\n{'─' * 40}")
    print("📊 Groundedness Evaluation:\n")
    print("  Pass Criteria:")
    for line in result["passed"]:
        print(line)
    for line in result["failed_pass"]:
        print(line)

    if result["triggered_fails"]:
        print("\n  Fail Criteria Triggered:")
        for line in result["triggered_fails"]:
            print(line)

    verdict = "✅ GROUNDED" if result["grounded"] else "❌ NOT GROUNDED"
    print(f"\n  Verdict: {verdict}")

    return result["grounded"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="~/.databrickscfg profile to use")
    parser.add_argument(
        "--test",
        choices=["T1_ANOMALY", "T2_VETO", "T3_TRANSFER", "ALL"],
        default="ALL",
        help="Which test(s) to run (default: ALL)",
    )
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    tests_to_run = (
        GOLDEN_TESTS
        if args.test == "ALL"
        else [t for t in GOLDEN_TESTS if t["id"] == args.test]
    )

    print(f"\n{'═' * 72}")
    print("  Manufacturing Inventory Intelligence Engine")
    print("  Genie Groundedness Validation — Golden Test Suite")
    print(f"{'═' * 72}")
    print(f"  Genie Space ID : {GENIE_SPACE_ID}")
    print(f"  Tests selected : {args.test}")
    print(f"  Running        : {len(tests_to_run)} test(s)")

    results = []
    for i, test in enumerate(tests_to_run, 1):
        grounded = run_test(w, test, i, len(tests_to_run))
        results.append((test["id"], test["name"], grounded))

    # Final summary
    print(f"\n{separator('═')}")
    print("  FINAL SUMMARY")
    print(separator())
    all_passed = True
    for tid, name, grounded in results:
        icon = "✅" if grounded else "❌"
        print(f"  {icon}  {tid}: {name}")
        if not grounded:
            all_passed = False

    print(separator())
    if all_passed:
        print("  🎯 ALL TESTS PASSED — Genie is reasoning through intelligence layers.")
        print("  ✅ Safe to proceed to quote schema and Supervisor integration.")
    else:
        print("  ⚠️  SOME TESTS FAILED — Genie is not fully grounded.")
        print("  🔧 Review the system prompt or example_question_sqls to reinforce the failing layers.")
    print(f"{separator('═')}\n")


if __name__ == "__main__":
    main()
