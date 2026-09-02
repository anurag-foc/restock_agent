"""Run the full phase-1 pipeline outside the Lakeflow job, from a laptop.

Mirrors `notebooks/lakeflow_trigger/invoke_supervisor.py` exactly -- same
pre-check + Turn 1 + one round-trip per candidate + final persist/notify
turn, same MCP approval round-trip, same post-hoc verification. Use it to
exercise a change end to end without waiting for the 07:00/15:00 schedule or
running the job.

What it deliberately does NOT do: persist the quote or send the Teams card
itself. An earlier version of this script called
`agentic_restock.quote_persistence.persist_quote` and
`integrations.teams_webhook.send_quote_card` directly, which pre-dated the
action-MCP server and violated the invariant in CLAUDE.md -- writes go through
an idempotent action tool, and nothing else. The Supervisor calls
`persist_quote` / `send_human_review` itself now (see
`mcp-inventory-actions/server/tools.py`); this script only verifies that it
did, the same way the notebook does.

Steps:
  1. Rebuild `inventory_signal_board` (the `refresh_signal_board` task's work).
  2. COUNT(*) over `rank_priority_actions(5)` -- a boolean fact about whether
     there is work, never the rows themselves. The Supervisor's only path to
     the answer stays a Genie tool call.
  3. COUNT(*) over `rank_priority_actions_diverse()` -- how many distinct
     signal types have a live top candidate (typically 1-3); drives how many
     analysis round-trips happen in step 4.
  4. Turn 1 (rank, diverse) -> one analysis turn per candidate -> final turn
     (persist all candidates in one quote + notify once).
  5. Verify a `quote_metadata` row landed, with one `fact_restock_request`
     line per candidate, and a Teams card went out.

Usage:
    PYTHONPATH=src python3 scripts/run_e2e_pipeline.py --profile anurag-r
    PYTHONPATH=src python3 scripts/run_e2e_pipeline.py --skip-board-refresh
    PYTHONPATH=src python3 scripts/run_e2e_pipeline.py --dry-run   # steps 1-3 only

This sends a real Teams card and writes a real quote when it reaches the
Supervisor conversation. Use --dry-run to stop before the Supervisor is
called at all.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

from agentic_restock.config import qualified_table
from agentic_restock.jobs.signal_board import BOARD_TABLE_NAME, build_signal_board_query

REPO_ROOT = Path(__file__).resolve().parent.parent
LAKEFLOW_JOB_YAML = REPO_ROOT / "resources/jobs/lakeflow_trigger_job.yml"
WAREHOUSE_ID = "d2533a75c1bd9265"

# Same ceiling as the notebook: Model Serving cuts the HTTP connection at ~290s.
TURN_TIMEOUT_SECONDS = 280

# Custom MCP tools always come back as an mcp_approval_request rather than
# executing; the endpoint is stateless, so continuing means resending the whole
# transcript plus the approval. See invoke_supervisor.py's helper for the full
# explanation -- this is a copy of it on purpose, so the two stay comparable.
MAX_APPROVAL_ROUNDS = 5


def resolve_endpoint_name() -> str:
    """Read the supervisor endpoint name from the Lakeflow job YAML.

    Not hardcoded here: endpoint names change every time the agent is
    re-created, and `scripts/ensure_supervisor_agent.py` rewrites this default
    in place across every file in its JOB_YAMLS list. Reading it back is how
    this script stays correct after a re-create without a second place to edit.
    """
    text = LAKEFLOW_JOB_YAML.read_text()
    match = re.search(r"name:\s*supervisor_endpoint_name\s*\n\s*default:\s*(\S+)", text)
    if not match:
        sys.exit(
            f"Could not find the supervisor_endpoint_name default in {LAKEFLOW_JOB_YAML}. "
            f"Run scripts/ensure_supervisor_agent.py, or pass --endpoint explicitly."
        )
    return match.group(1)


def run_sql(w: WorkspaceClient, statement: str, description: str):
    """Execute a statement, polling until it finishes.

    The board rebuild is a CREATE OR REPLACE TABLE over the full fact history
    and routinely outruns the 50s `wait_timeout` ceiling, so this polls rather
    than assuming one round-trip is enough.
    """
    res = w.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="50s",
    )
    while res.status and res.status.state and res.status.state.value in ("PENDING", "RUNNING"):
        time.sleep(5)
        res = w.statement_execution.get_statement(res.statement_id)
    state = res.status.state.value if res.status and res.status.state else "UNKNOWN"
    if state != "SUCCEEDED":
        error = res.status.error.message if res.status and res.status.error else "(no error message)"
        sys.exit(f"{description} failed ({state}): {error}")
    return res


def scalar(res) -> str | None:
    rows = res.result.data_array if res.result else None
    return rows[0][0] if rows else None


def invoke(w: WorkspaceClient, endpoint: str, messages: list[dict]) -> str:
    """POST the message history and return the final assistant text.

    Answers any mcp_approval_request within the same turn by resending the
    transcript plus the approval -- the documented way to consume this API.
    """
    current_input = list(messages)
    response = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={"input": current_input},
    )
    for _ in range(MAX_APPROVAL_ROUNDS):
        approval_requests = [item for item in response.get("output", []) if item.get("type") == "mcp_approval_request"]
        if not approval_requests:
            break
        current_input = current_input + response["output"] + [
            {"type": "mcp_approval_response", "approval_request_id": item["id"], "approve": True}
            for item in approval_requests
        ]
        response = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint}/invocations",
            body={"input": current_input},
        )
    text = ""
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text = part["text"]
    return text or json.dumps(response, indent=2)


TURN1_PROMPT = (
    "You are running the Manufacturing Inventory Intelligence Engine scan.\n\n"
    "Call `rank_priority_actions_diverse` to see the top-ranked action for each distinct "
    "signal type live this run.\n\n"
    "Output ONLY the picked rows' own columns, verbatim, one row per line, in the order "
    "returned: part_id, warehouse_id, signal_type, exposure, action_cost, decision_value, "
    "commitment_state, commitment_age_days. "
    "Do not analyse them, do not call any other tool, and do not call `persist_quote` or "
    "`send_human_review` yet."
)


def turn2_prompt(i: int, n: int) -> str:
    return (
        f"Now analyse the {i}-th of the {n} candidates you selected in Turn 1 (same order).\n\n"
        "1. Pull whatever supporting detail you need from `scan_transfer_options`, "
        "`scan_assembly_risk`, `evaluate_suppliers`, and `evaluate_feasibility` -- pick "
        "only the one or two most relevant to its signal_type, plus the "
        "`inventory_signal_board` table itself for the part's current on-hand quantity, "
        "safety stock, and target stock level.\n"
        "1b. Then ALWAYS call `scan_demand_shift` and `scan_leadtime_drift` for this "
        "part, on top of whatever you picked above. These two are corrections, not "
        "extras: `scan_demand_shift` returns the seasonal multiplier on the burn rate "
        "and `scan_leadtime_drift` returns how far the supplier's real delivery record "
        "has drifted from its contracted lead time. If either returns a row for this "
        "part, use its numbers and cite it in EVIDENCE -- a burn rate or a lead time "
        "that is quietly wrong makes every other figure in the artifact wrong, so it "
        "cannot be left out. If a function returns nothing for this part, say so in one "
        "short EVIDENCE line rather than omitting it silently.\n"
        "2. Decide a single resolution: transfer, purchase order, or (if genuinely nothing "
        "helps) an escalation with no action attached.\n"
        "3. Emit the full artifact in the OUTPUT CONTRACT format, stating the recommended "
        "action, quantity, supplier (if a purchase), and the Rs cost of acting vs the Rs "
        "cost of doing nothing -- both sides, not just one. IF APPROVED AND WRONG must be "
        "the real cost of the chosen option (quantity x unit_cost, plus excess_holding_cost "
        "if any) with the multiplication shown inline -- never the ranking's action_cost or "
        "decision_value figure, and never a number that doesn't sum to what you write. Start "
        "this artifact with the "
        f"exact line `## CANDIDATE {i} of {n} -- <signal_type>` (fill in the actual "
        "signal_type) so it can be told apart from the other candidates' analyses later.\n\n"
        "Analyse only this one candidate -- do not analyse any other candidate in this turn. "
        "Do not call `persist_quote` or `send_human_review` yet -- wait for the final "
        "instruction."
    )


def turn_final_prompt(n: int) -> str:
    return (
        f"You analysed {n} candidate(s) above. Now persist and notify -- required, in this "
        "order.\n\n"
        "1. Call `persist_quote` with:\n"
        f"   - `candidates_json`: a JSON array with one object PER candidate you analysed "
        f"({n} object(s) total), each with fields item_id, warehouse_id, current_stock_qty, "
        "reorder_point_qty, suggested_reorder_qty, initial_urgency -- look these up from "
        "`inventory_signal_board` if you have not already.\n"
        "   - `summary_report`: all of the OUTPUT CONTRACT artifacts you wrote above, "
        f"concatenated in the same order, each still starting with its own `## CANDIDATE i "
        f"of {n}` marker line.\n"
        "   It returns a `quote_id`.\n"
        "2. Call `send_human_review` with that returned `quote_id` and the same summary "
        "text. It builds the Review App link itself -- do not pass a review_url.\n\n"
        "Use the id persist_quote actually returns -- never invent one. Both tools are "
        "idempotent, so call each exactly once and read the result."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="anurag-r", help="~/.databrickscfg profile (default: anurag-r)")
    parser.add_argument("--endpoint", help="Supervisor endpoint name (default: read from the Lakeflow job YAML)")
    parser.add_argument(
        "--skip-board-refresh",
        action="store_true",
        help="Reuse the existing inventory_signal_board instead of rebuilding it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refresh the board and report the candidate counts, but do not call the Supervisor",
    )
    args = parser.parse_args()

    endpoint_name = args.endpoint or resolve_endpoint_name()
    board = qualified_table(BOARD_TABLE_NAME)
    quote_metadata = qualified_table("quote_metadata")
    fact_restock_request = qualified_table("fact_restock_request")
    ranking = qualified_table("rank_priority_actions")
    ranking_diverse = qualified_table("rank_priority_actions_diverse")

    w = WorkspaceClient(
        profile=args.profile,
        config=Config(http_timeout_seconds=TURN_TIMEOUT_SECONDS, retry_timeout_seconds=300),
    )

    print("Running the phase-1 pipeline outside the job.")
    print(f"  profile:  {args.profile}")
    print(f"  endpoint: {endpoint_name}")

    # ── Step 1: rebuild the signal board ─────────────────────────────────
    if args.skip_board_refresh:
        print(f"\n[1/5] Skipping board refresh; reusing {board}.")
    else:
        print(f"\n[1/5] Rebuilding {board} (this scans the full fact history)...")
        run_sql(w, build_signal_board_query(), "Signal board refresh")
        rows = scalar(run_sql(w, f"SELECT COUNT(*) FROM {board}", "Board row count"))
        print(f"      rebuilt: {int(rows):,} part/warehouse rows.")

    # ── Step 2: is there anything to do? ─────────────────────────────────
    print("\n[2/5] Counting actions that clear the materiality floor...")
    candidate_count = int(scalar(run_sql(w, f"SELECT COUNT(*) FROM {ranking}(5)", "Priority ranking count")))
    print(f"      {candidate_count} action(s) cleared the ranking.")

    if candidate_count == 0:
        print("\nNo action needed. Nothing sent to the Supervisor (the job logs this as NO_ACTION).")
        return

    # ── Step 3: how many distinct signal types are live? ──────────────────
    print("\n[3/5] Counting distinct signal types with a live top candidate...")
    diverse_count = int(scalar(run_sql(w, f"SELECT COUNT(*) FROM {ranking_diverse}()", "Diverse ranking count")))
    print(f"      {diverse_count} distinct signal type(s) live this run.")

    if args.dry_run:
        print("\n--dry-run: stopping before the Supervisor call.")
        return

    # ── Step 4: Turn 1, one analysis turn per candidate, final turn ───────
    print("\n[4/5] Supervisor conversation.")
    messages = [{"role": "user", "content": TURN1_PROMPT}]

    print("      Turn 1 -- priority scan and selection...")
    selection_text = invoke(w, endpoint_name, messages)
    messages.append({"role": "assistant", "content": selection_text})
    print(f"\n--- Turn 1 ---\n{selection_text}\n")

    for i in range(1, diverse_count + 1):
        messages.append({"role": "user", "content": turn2_prompt(i, diverse_count)})
        print(f"      Turn 2.{i} -- analysis and resolution for candidate {i} of {diverse_count}...")
        analysis_text = invoke(w, endpoint_name, messages)
        messages.append({"role": "assistant", "content": analysis_text})
        print(f"\n--- Turn 2.{i} ---\n{analysis_text}\n")

    messages.append({"role": "user", "content": turn_final_prompt(diverse_count)})
    print("      Final turn -- persist and notify...")
    final_text = invoke(w, endpoint_name, messages)
    print(f"\n--- Final turn ---\n{final_text}\n")

    # ── Step 5: verify the action tools actually ran ──────────────────────
    #
    # Same check the notebook makes, for the same reason: a silent no-write is
    # the main failure mode of putting an action in an LLM's hands. The tools
    # are idempotent, so this never retries them -- it only reports.
    print("[5/5] Verifying the Supervisor persisted and notified...")
    quote_id = scalar(run_sql(
        w,
        f"SELECT quote_id FROM {quote_metadata} "
        f"WHERE created_at >= current_timestamp() - INTERVAL 1 HOUR "
        f"ORDER BY created_at DESC LIMIT 1",
        "quote_metadata lookup",
    ))

    if not quote_id:
        sys.exit(
            "FAILED: the Supervisor did not call persist_quote -- no quote_metadata row was "
            "written in the last hour. The analysis is above, but nothing was saved and no "
            "reviewer was notified."
        )
    lines_written = int(scalar(run_sql(
        w,
        f"SELECT COUNT(*) FROM {fact_restock_request} WHERE QUOTE_ID = '{quote_id}'",
        "fact_restock_request line count",
    )))
    print(f"      persisted quote: {quote_id} ({lines_written} line(s), expected {diverse_count})")

    teams_message_id = scalar(run_sql(
        w,
        f"SELECT teams_message_id FROM {quote_metadata} WHERE quote_id = '{quote_id}'",
        "Teams notification lookup",
    ))
    if teams_message_id:
        print(f"      review notification sent: {teams_message_id}")
        print("\nDone. Check Teams for the card, then open the Review App to approve or reject.")
    else:
        print(
            f"      WARNING: quote {quote_id} was persisted but send_human_review did not run -- "
            f"no Teams card was sent, so nobody has been asked to review it."
        )


if __name__ == "__main__":
    main()
