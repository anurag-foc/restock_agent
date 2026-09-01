# Databricks notebook source
# MAGIC %md
# MAGIC # Invoke Supervisor Agent
# MAGIC
# MAGIC Runs after `refresh_signal_board` has rebuilt
# MAGIC `gold_dev.supply_chain_analytics.inventory_signal_board`. Replaces the old
# MAGIC multi-turn "one candidate per turn" protocol: `refresh_signal_board`
# MAGIC already told us how many actions cleared `rank_priority_actions`'
# MAGIC materiality floor, so there is no candidate list to loop over here --
# MAGIC there is at most a handful of ranked actions, and the Supervisor picks
# MAGIC the single priority one itself, through Genie, in its own turn.
# MAGIC
# MAGIC This notebook deliberately does NOT fetch the ranked rows and hand them
# MAGIC to the Supervisor as pre-chewed JSON. An earlier revision of this
# MAGIC pipeline attached the deep-analysis UC functions directly to the
# MAGIC Supervisor instead of behind Genie, and the Supervisor then reasoned
# MAGIC straight from candidate JSON and never called Genie at all -- defeating
# MAGIC the design (see CLAUDE.md). The fix that stuck was structural: the
# MAGIC Supervisor's only path to analysis is a Genie tool call, so the only way
# MAGIC to keep it that way here is to genuinely not know the answer ourselves.
# MAGIC The most this notebook checks is a COUNT(*), which is a boolean fact
# MAGIC ("is there anything to do"), not a judgment.
# MAGIC
# MAGIC ## Turn Sequence
# MAGIC   Turn 1 -- "Run the priority scan, pick the top action." (Genie tool
# MAGIC             call: rank_priority_actions only.)
# MAGIC   Turn 2 -- "Analyse the picked action, decide a resolution." (Genie
# MAGIC             tool calls: whichever drill-down functions that action needs.)
# MAGIC   Turn 3 -- "Persist and notify." (MCP tool calls: persist_quote,
# MAGIC             send_human_review.)
# MAGIC
# MAGIC An earlier revision of this notebook combined Turn 1 and Turn 2 into one
# MAGIC round-trip ("scan, analyse, and decide" in a single turn). That call
# MAGIC chains rank_priority_actions plus one or more drill-down Genie calls plus
# MAGIC the model's own write-up, which comfortably exceeds the ~290s Model
# MAGIC Serving gateway ceiling and fails with `TimeoutError: Timed out after
# MAGIC 0:05:00` -- the same collapse-into-one-big-prompt failure the old
# MAGIC N-candidate loop was designed to avoid, just re-triggered per-turn instead
# MAGIC of per-candidate. Splitting the scan from the analysis keeps each turn to
# MAGIC at most a couple of Genie calls, which reliably clears the ceiling.

# COMMAND ----------

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor Agent serving endpoint name")

# COMMAND ----------

import sys

sys.path.append("../../src")

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

from agentic_restock.jobs.run_log import build_run_log_insert, build_run_log_table_ddl

endpoint_name = dbutils.widgets.get("supervisor_endpoint_name")

# Counted here rather than in refresh_signal_board, which cannot call this
# function without deadlocking a fresh workspace (see that notebook's comment).
# This is a COUNT(*) and nothing more -- a boolean fact about whether there is
# work, not a judgment about what the work is. The ranked rows themselves are
# deliberately not read here; the Supervisor calls rank_priority_actions fresh
# through Genie so its only path to the answer stays a Genie tool call.
candidate_count = spark.sql(
    "SELECT COUNT(*) c FROM gold_dev.supply_chain_analytics.rank_priority_actions(5)"
).collect()[0]["c"]
print(f"{candidate_count} action(s) cleared the priority ranking's materiality floor this run.")

# COMMAND ----------

# Deterministic short-circuit. Whether the list is empty is a boolean fact,
# not a judgment -- routing it through the Supervisor would invite it to
# narrate uncertainty or invent something to act on over an empty set. This
# is recorded either way (see run_log.py) so "nothing needed attention today"
# is distinguishable from "the job silently broke", and so the no-action rate
# is a reportable number, not an absence of evidence.

spark.sql(build_run_log_table_ddl())

if candidate_count == 0:
    spark.sql(build_run_log_insert(candidate_count=0, outcome="NO_ACTION", note="No candidate cleared the materiality floor"))
    print("No action needed this run. Nothing sent to the Supervisor.")
    dbutils.notebook.exit("NO_ACTION")

if not endpoint_name:
    print(
        "No supervisor_endpoint_name job parameter set -- skipping the live call. "
        "Set it to the Supervisor Agent's endpoint_name (e.g. mas-<id>-endpoint, "
        "printed by scripts/create_supervisor_agent.py) to invoke it for real."
    )
    dbutils.notebook.exit("NO_ENDPOINT_CONFIGURED")

spark.sql(build_run_log_insert(candidate_count=candidate_count, outcome="SUPERVISOR_INVOKED"))

# COMMAND ----------

# ── Helper: invoke the endpoint and extract the assistant text ─────────────────
#
# Custom MCP tools (attached via the `app` tool type -- persist_quote,
# send_human_review) always come back from the Responses API as an
# mcp_approval_request instead of executing. There is no supported way to
# disable this at tool-registration or per-request time (both checked; neither
# is honored) -- Databricks requires an explicit approval round-trip for any
# custom-MCP tool call. This endpoint is stateless (no session to resume, see
# docs/agent_bricks_mapping.md §2.5), so `previous_response_id` chaining does
# NOT work here -- confirmed by testing: it returns "Invalid message sequence.
# The approval response was in an unexpected position." Continuing instead
# means resending the full transcript as `input`: everything sent so far, plus
# every item from the prior response's `output` verbatim, plus the
# mcp_approval_response. This is the documented way to consume that API, not a
# workaround, so _invoke answers it immediately within the same turn.
MAX_APPROVAL_ROUNDS = 5


def _invoke(w, endpoint, messages):
    """POST the current message history to the serving endpoint and return
    the final assistant text from the response output array."""
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
    if not text:
        # Fallback: surface the raw response so failures are visible in logs
        text = json.dumps(response, indent=2)
    return text

# COMMAND ----------

# Ambient auth inside a Databricks job/notebook -- no explicit host/token needed.
# Per-turn HTTP timeout is 280s (just under the 290s gateway ceiling).
w = WorkspaceClient(config=Config(http_timeout_seconds=280, retry_timeout_seconds=300))

# ══════════════════════════════════════════════════════════════════════════
# TURN 1 -- Priority scan and selection ONLY
#
# A single Genie call. Nothing here tells it which action to pick -- that is
# the point. Deliberately kept to one tool call: a Turn that also drills into
# the picked action (multiple further Genie calls) plus writes up the full
# analysis was tried first and timed out -- see the module docstring above.
# ══════════════════════════════════════════════════════════════════════════

turn1_prompt = (
    "You are running the Manufacturing Inventory Intelligence Engine scan.\n\n"
    "Call `rank_priority_actions` to see this run's ranked candidates, and pick the "
    "top-ranked action by decision_value, unless you have a specific reason not to.\n\n"
    "Output ONLY the picked row's own columns, verbatim: part_id, warehouse_id, signal_type, "
    "exposure, action_cost, decision_value, commitment_state, commitment_age_days. "
    "Do not analyse it, do not call any other tool, and do not call `persist_quote` or "
    "`send_human_review` yet."
)

messages = [{"role": "user", "content": turn1_prompt}]

print("\nTurn 1 -- priority scan and selection...")
selection_text = _invoke(w, endpoint_name, messages)
messages.append({"role": "assistant", "content": selection_text})
print(f"   done ({len(selection_text)} chars).")
print(selection_text)

# ══════════════════════════════════════════════════════════════════════════
# TURN 2 -- Analysis and resolution for the picked action
#
# The Supervisor calls Genie for whichever of the six drill-down functions
# the picked action needs (scan_transfer_options / scan_assembly_risk /
# scan_leadtime_drift / evaluate_suppliers / evaluate_feasibility) -- one or
# two calls, not all of them reflexively.
# ══════════════════════════════════════════════════════════════════════════

turn2_prompt = (
    "Now analyse the action you just picked.\n\n"
    "1. Pull whatever supporting detail you need from `scan_transfer_options`, "
    "`scan_assembly_risk`, `scan_leadtime_drift`, `evaluate_suppliers`, and "
    "`evaluate_feasibility` -- pick only the one or two most relevant to its "
    "signal_type, plus the `inventory_signal_board` table itself for the part's "
    "current on-hand quantity, safety stock, and target stock level.\n"
    "2. Decide a single resolution: transfer, purchase order, or (if genuinely nothing "
    "helps) an escalation with no action attached.\n"
    "3. Emit the full artifact in the OUTPUT CONTRACT format, stating the recommended "
    "action, quantity, supplier (if a purchase), and the Rs cost of acting vs the Rs "
    "cost of doing nothing -- both sides, not just one.\n\n"
    "Do not call `persist_quote` or `send_human_review` yet -- wait for the next "
    "instruction."
)

messages.append({"role": "user", "content": turn2_prompt})

print("\nTurn 2 -- analysis and resolution...")
analysis_text = _invoke(w, endpoint_name, messages)
messages.append({"role": "assistant", "content": analysis_text})
print(f"   done ({len(analysis_text)} chars).")
print(analysis_text)

# ══════════════════════════════════════════════════════════════════════════
# TURN 3 -- Persist and notify
#
# persist_quote's current signature (candidates_json: item_id,
# warehouse_id, current_stock_qty, reorder_point_qty, suggested_reorder_qty,
# initial_urgency) predates this redesign and still expects that shape, not
# the new rank_priority_actions output. Rather than build a shim here, the
# Supervisor is told to assemble candidates_json from what it already looked
# up on the board in Turn 2 -- Genie can query inventory_signal_board
# directly for exactly those fields. Updating persist_quote's contract to
# accept the new action shape natively is a real follow-up, not done here.
# ══════════════════════════════════════════════════════════════════════════

turn3_prompt = (
    "Now persist and notify -- required, in this order.\n\n"
    "1. Call `persist_quote` with:\n"
    "   - `candidates_json`: a JSON array with one object for the action you just "
    "analysed, with fields item_id, warehouse_id, current_stock_qty, "
    "reorder_point_qty, suggested_reorder_qty, initial_urgency -- look these up from "
    "`inventory_signal_board` if you have not already.\n"
    "   - `summary_report`: the full analysis you just wrote, including both the "
    "recommended action and the cost of doing nothing.\n"
    "   It returns a `quote_id`.\n"
    "2. Call `send_human_review` with that returned `quote_id` and the same summary "
    "text. It builds the Review App link itself -- do not pass a review_url.\n\n"
    "Use the id persist_quote actually returns -- never invent one. Both tools are "
    "idempotent, so call each exactly once and read the result."
)

messages.append({"role": "user", "content": turn3_prompt})

print("\nTurn 3 -- persist and notify...")
final_text = _invoke(w, endpoint_name, messages)
print(final_text)

dbutils.jobs.taskValues.set(key="supervisor_response", value=final_text)

# ── Verify the Supervisor actually persisted and notified ─────────────────
#
# Persistence and the Teams card are done by the Supervisor itself, via the
# persist_quote and send_human_review MCP tools (see
# mcp-inventory-actions/server/tools.py and scripts/create_supervisor_agent.py).
# The tools are idempotent, so this notebook does NOT retry them -- it only
# checks that they ran, and fails loudly if not. A silent no-write is the
# main failure mode of moving an action into an LLM's hands, so it is
# surfaced as a task failure rather than a log line nobody reads.

quote_ids = [row["quote_id"] for row in spark.sql("""
    SELECT quote_id
    FROM gold_dev.supply_chain_analytics.quote_metadata
    WHERE created_at >= current_timestamp() - INTERVAL 1 HOUR
    ORDER BY created_at DESC
    LIMIT 1
""").collect()]

if not quote_ids:
    raise RuntimeError(
        "Supervisor did not call persist_quote -- no quote_metadata row was written in the "
        "last hour. The analysis is in the supervisor_response task value, but nothing "
        "was saved and no reviewer was notified."
    )

quote_id = quote_ids[0]
dbutils.jobs.taskValues.set(key="quote_id", value=quote_id)
print(f"\nSupervisor persisted quote: {quote_id}")

notified = spark.sql(f"""
    SELECT teams_message_id
    FROM gold_dev.supply_chain_analytics.quote_metadata
    WHERE quote_id = '{quote_id}'
""").collect()[0]["teams_message_id"]

if notified:
    print(f"Supervisor sent the review notification: {notified}")
else:
    print(
        f"WARNING: quote {quote_id} was persisted but send_human_review did not run -- "
        f"no Teams card was sent, so nobody has been asked to review it."
    )
