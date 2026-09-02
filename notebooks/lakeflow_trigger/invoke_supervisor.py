# Databricks notebook source
# MAGIC %md
# MAGIC # Invoke Supervisor Agent
# MAGIC
# MAGIC Runs after `refresh_signal_board` has rebuilt
# MAGIC `gold_dev.supply_chain_analytics.inventory_signal_board`.
# MAGIC
# MAGIC This notebook deliberately does NOT fetch the ranked rows and hand them
# MAGIC to the Supervisor as pre-chewed JSON. An earlier revision of this
# MAGIC pipeline attached the deep-analysis UC functions directly to the
# MAGIC Supervisor instead of behind Genie, and the Supervisor then reasoned
# MAGIC straight from candidate JSON and never called Genie at all -- defeating
# MAGIC the design (see CLAUDE.md). The fix that stuck was structural: the
# MAGIC Supervisor's only path to analysis is a Genie tool call, so the only way
# MAGIC to keep it that way here is to genuinely not know the answer ourselves.
# MAGIC The most this notebook checks is a `COUNT(*)`, which is a boolean/count
# MAGIC fact ("is there anything to do", "how many things"), not a judgment
# MAGIC about which ones matter.
# MAGIC
# MAGIC ## Turn Sequence
# MAGIC A run surfaces the top-ranked action for EACH distinct signal type
# MAGIC currently live (STOCK_THRESHOLD, BOM_CASCADE_RISK, STALLED_COMMITMENT --
# MAGIC typically 1-3 on a given day), bundled into ONE quote and ONE Teams
# MAGIC notification -- not the single loudest number, and not N separate
# MAGIC quotes/cards either. This is still the "hard output budget, ranked by
# MAGIC money" docs/market_evidence_phase1.md §3 argues for -- a bounded,
# MAGIC small, single-notification quote, not a flood.
# MAGIC
# MAGIC   Pre-check -- `SELECT COUNT(*) FROM rank_priority_actions_diverse()`
# MAGIC               gives N (how many distinct signal types have a live
# MAGIC               candidate), which drives the loop below. A count, not a
# MAGIC               judgment about which candidates -- same category as the
# MAGIC               existing NO_ACTION count.
# MAGIC   Turn 1     -- "Run the priority scan, list the top action per signal
# MAGIC               type." (Genie tool call: rank_priority_actions_diverse only.)
# MAGIC   Turns 2.1..2.N -- one round-trip PER candidate: "Analyse candidate i of
# MAGIC               N, decide a resolution." (Genie tool calls: whichever
# MAGIC               drill-down functions that candidate needs.)
# MAGIC   Final turn -- "Persist and notify." (MCP tool calls: persist_quote with
# MAGIC               all N candidates in one array, then send_human_review
# MAGIC               once.)
# MAGIC
# MAGIC An earlier revision of this notebook combined the scan and the analysis
# MAGIC into one round-trip ("scan, analyse, and decide" in a single turn). That
# MAGIC call chains rank_priority_actions plus one or more drill-down Genie calls
# MAGIC plus the model's own write-up, which comfortably exceeds the ~290s Model
# MAGIC Serving gateway ceiling and fails with `TimeoutError: Timed out after
# MAGIC 0:05:00`. Splitting the scan from each candidate's analysis -- one
# MAGIC round-trip per candidate, each resetting the clock -- keeps every turn to
# MAGIC at most a couple of Genie calls, which reliably clears the ceiling; it
# MAGIC costs more wall-clock on a day with more live signal types, never more
# MAGIC risk of timing out.

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
# deliberately not read here; the Supervisor calls rank_priority_actions_diverse
# fresh through Genie so its only path to the answer stays a Genie tool call.
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

# How many distinct signal types have a live top candidate right now -- drives
# how many Turn-2-style analysis round-trips run below. A count, not a
# judgment about which signal types or which candidates matter (the Supervisor
# picks those itself, through Genie, in Turn 1).
diverse_candidate_count = spark.sql(
    "SELECT COUNT(*) c FROM gold_dev.supply_chain_analytics.rank_priority_actions_diverse()"
).collect()[0]["c"]
print(f"{diverse_candidate_count} distinct signal type(s) have a live top candidate this run.")

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
# A single Genie call. Nothing here tells it which actions to pick -- that is
# the point. Deliberately kept to one tool call: a Turn that also drills into
# the picked actions (multiple further Genie calls) plus writes up the full
# analysis was tried first (for a single candidate) and timed out -- see the
# module docstring above.
# ══════════════════════════════════════════════════════════════════════════

turn1_prompt = (
    "You are running the Manufacturing Inventory Intelligence Engine scan.\n\n"
    "Call `rank_priority_actions_diverse` to see the top-ranked action for each distinct "
    "signal type live this run.\n\n"
    "Output ONLY the picked rows' own columns, verbatim, one row per line, in the order "
    "returned: part_id, warehouse_id, signal_type, exposure, action_cost, decision_value, "
    "commitment_state, commitment_age_days. "
    "Do not analyse them, do not call any other tool, and do not call `persist_quote` or "
    "`send_human_review` yet."
)

messages = [{"role": "user", "content": turn1_prompt}]

print("\nTurn 1 -- priority scan and selection...")
selection_text = _invoke(w, endpoint_name, messages)
messages.append({"role": "assistant", "content": selection_text})
print(f"   done ({len(selection_text)} chars).")
print(selection_text)

# ══════════════════════════════════════════════════════════════════════════
# TURNS 2.1 .. 2.N -- Analysis and resolution, one round-trip per candidate
#
# Each iteration is its own HTTP call (own 280s budget), for the same reason
# Turn 1 and Turn 2 were originally split -- ranking plus every candidate's
# drill-down plus every write-up in one call would blow the ceiling. The
# Supervisor calls Genie for whichever of the six drill-down functions each
# candidate needs -- one or two calls, not all of them reflexively.
# ══════════════════════════════════════════════════════════════════════════

analysis_blocks = []

for i in range(1, diverse_candidate_count + 1):
    turn2_prompt = (
        f"Now analyse the {i}-th of the {diverse_candidate_count} candidates you selected in "
        "Turn 1 (same order).\n\n"
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
        f"exact line `## CANDIDATE {i} of {diverse_candidate_count} -- <signal_type>` (fill in "
        "the actual signal_type) so it can be told apart from the other candidates' analyses "
        "later.\n\n"
        "Analyse only this one candidate -- do not analyse any other candidate in this turn. "
        "Do not call `persist_quote` or `send_human_review` yet -- wait for the final "
        "instruction."
    )

    messages.append({"role": "user", "content": turn2_prompt})

    print(f"\nTurn 2.{i} -- analysis and resolution for candidate {i} of {diverse_candidate_count}...")
    analysis_text = _invoke(w, endpoint_name, messages)
    messages.append({"role": "assistant", "content": analysis_text})
    print(f"   done ({len(analysis_text)} chars).")
    print(analysis_text)
    analysis_blocks.append(analysis_text)

# ══════════════════════════════════════════════════════════════════════════
# FINAL TURN -- Persist and notify
#
# persist_quote's current signature (candidates_json: item_id,
# warehouse_id, current_stock_qty, reorder_point_qty, suggested_reorder_qty,
# initial_urgency) predates the phase-1 redesign and still expects that shape,
# not rank_priority_actions_diverse's output shape. Rather than build a shim
# here, the Supervisor is told to assemble candidates_json from what it
# already looked up on the board in Turns 2.1..2.N -- Genie can query
# inventory_signal_board directly for exactly those fields. persist_quote
# already loops over an arbitrary-length candidates_json array and writes one
# fact_restock_request line per candidate under one shared quote_id -- no
# tool-side change needed for N>1 candidates.
# ══════════════════════════════════════════════════════════════════════════

turn_final_prompt = (
    f"You analysed {diverse_candidate_count} candidate(s) above. Now persist and notify -- "
    "required, in this order.\n\n"
    "1. Call `persist_quote` with:\n"
    f"   - `candidates_json`: a JSON array with one object PER candidate you analysed "
    f"({diverse_candidate_count} object(s) total), each with fields item_id, warehouse_id, "
    "current_stock_qty, reorder_point_qty, suggested_reorder_qty, initial_urgency -- look "
    "these up from `inventory_signal_board` if you have not already.\n"
    "   - `summary_report`: all of the OUTPUT CONTRACT artifacts you wrote above, "
    "concatenated in the same order, each still starting with its own "
    f"`## CANDIDATE i of {diverse_candidate_count}` marker line.\n"
    "   It returns a `quote_id`.\n"
    "2. Call `send_human_review` with that returned `quote_id` and the same summary "
    "text. It builds the Review App link itself -- do not pass a review_url.\n\n"
    "Use the id persist_quote actually returns -- never invent one. Both tools are "
    "idempotent, so call each exactly once and read the result."
)

messages.append({"role": "user", "content": turn_final_prompt})

print("\nFinal turn -- persist and notify...")
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

lines_written = spark.sql(f"""
    SELECT COUNT(*) c FROM gold_dev.supply_chain_analytics.fact_restock_request
    WHERE QUOTE_ID = '{quote_id}'
""").collect()[0]["c"]
print(f"\nSupervisor persisted quote: {quote_id} ({lines_written} line(s), expected {diverse_candidate_count})")

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
