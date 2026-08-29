# Databricks notebook source
# MAGIC %md
# MAGIC # Invoke Supervisor Agent
# MAGIC
# MAGIC Runs only when `has_candidates` is true, i.e. the §4.1 coarse check found
# MAGIC at least one item below its reorder point. Hands the candidate list off to
# MAGIC the real Supervisor Agent endpoint (see `scripts/create_supervisor_agent.py`
# MAGIC and `docs/agent_bricks_mapping.md`) and lets it drive everything from there:
# MAGIC Genie deep-analysis (via the §4.2 Unity Catalog functions), the veto
# MAGIC decision, and — once built — writing `gold_dev.supply_chain_analytics.
# MAGIC fact_restock_request` + `ab_training.agentic_restock.quote_metadata` and
# MAGIC sending the Teams Adaptive Card.
# MAGIC
# MAGIC The deep analysis itself (consumption trend, stockout forecast, urgency,
# MAGIC veto, quote) is **not** done here. It lives in the Unity Catalog functions
# MAGIC in `ab_training.agentic_restock` that the Supervisor Agent and Genie Agent
# MAGIC call as tools — those functions read Data Engineering's `gold_dev` star
# MAGIC schema (`fact_inventory_snapshot`, `fact_inventory_transaction`,
# MAGIC `fact_procurement`) under the hood.
# MAGIC
# MAGIC ## Multi-Turn Orchestration Protocol
# MAGIC
# MAGIC To stay within the 290-second HTTP gateway ceiling on the Model Serving
# MAGIC endpoint, this notebook uses a sequential multi-turn conversation instead
# MAGIC of a single large prompt. Each HTTP request is a separate turn that carries
# MAGIC one candidate plus the accumulated message history -- so the agent retains
# MAGIC full cross-candidate context (surplus allocation, supplier consolidation)
# MAGIC while every individual request completes in ~60-80 seconds.
# MAGIC
# MAGIC Turn sequence:
# MAGIC   Turn 0  -- Opening briefing: total candidate summary + session instructions
# MAGIC   Turn 1..N -- Per-candidate analysis (one item per turn, Genie tool calls)
# MAGIC   [Compression] -- If N > COMPRESSION_THRESHOLD, collapse history to summaries
# MAGIC   Turn N+1 -- Explicit quote creation: assemble consolidated Restock Quote

# COMMAND ----------

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor Agent serving endpoint name")
dbutils.widgets.text("teams_webhook_url", "", "Teams Webhook URL (optional, defaults to TEAMS_WEBHOOK_URL env var)")

# COMMAND ----------

import json
import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

candidates_json = dbutils.jobs.taskValues.get(
    taskKey="coarse_check", key="candidates_json", default="[]", debugValue="[]"
)
candidates = json.loads(candidates_json)
endpoint_name = dbutils.widgets.get("supervisor_endpoint_name")
teams_webhook_url = dbutils.widgets.get("teams_webhook_url") or os.environ.get("TEAMS_WEBHOOK_URL") or None

print(f"{len(candidates)} candidate(s) from the coarse check:")
for c in candidates:
    print(
        f"  - [{c.get('signal_type', 'STOCK_THRESHOLD')} | {c.get('initial_urgency', 'CRITICAL')}] "
        f"{c['item_id']} @ {c['warehouse_id']}: {c['current_stock_qty']} on hand "
        f"(reorder point {c['reorder_point_qty']})"
    )

# COMMAND ----------

# ── Configuration ─────────────────────────────────────────────────────────────

# When the batch exceeds this many candidates the notebook compresses the
# accumulated per-candidate analyses into concise summaries before the final
# quote-creation turn, keeping the input token count manageable.
COMPRESSION_THRESHOLD = 4

# ── Helper: invoke the endpoint and extract the assistant text ─────────────────

def _invoke(w, endpoint, messages):
    """POST the current message history to the serving endpoint and return
    the final assistant text from the response output array."""
    response = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body={"input": messages},
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

if not endpoint_name:
    print(
        "No supervisor_endpoint_name job parameter set -- skipping the live call. "
        "Set it to the Supervisor Agent's endpoint_name (e.g. mas-<id>-endpoint, "
        "printed by scripts/create_supervisor_agent.py) to invoke it for real."
    )
else:
    # Ambient auth inside a Databricks job/notebook -- no explicit host/token needed.
    # Per-turn HTTP timeout is 280s (just under the 290s gateway ceiling).
    # Each turn is a separate request so the clock resets between candidates.
    w = WorkspaceClient(config=Config(http_timeout_seconds=280, retry_timeout_seconds=300))

    # ── Candidate index (shown once in briefing, shared across all turns) ─────

    candidate_index_lines = []
    for i, c in enumerate(candidates, 1):
        sig  = c.get("signal_type", "STOCK_THRESHOLD")
        urg  = c.get("initial_urgency", "CRITICAL")
        days = f", {c['days_to_stockout']}d to stockout" if c.get("days_to_stockout") else ""
        asm  = f", threatens {c['threatened_assembly']}" if c.get("threatened_assembly") else ""
        candidate_index_lines.append(
            f"  {i}. [{sig} | {urg}] {c['item_id']} ({c.get('item_name', '')}) "
            f"@ {c['warehouse_id']}: stock={c['current_stock_qty']}, "
            f"reorder={c['reorder_point_qty']}{days}{asm}"
        )
    candidate_index = "\n".join(candidate_index_lines)

    # ══════════════════════════════════════════════════════════════════════════
    # TURN 0 -- Opening Briefing
    # Gives the Supervisor a session-level view of all candidates before any
    # individual analysis begins. No Genie calls expected on this turn.
    # ══════════════════════════════════════════════════════════════════════════

    briefing_prompt = (
        f"You are beginning a multi-turn intelligence session for the Manufacturing "
        f"Inventory Intelligence Engine.\n\n"
        f"The Lakeflow multi-signal scanner flagged {len(candidates)} candidate(s) "
        f"this run. Each candidate will be delivered to you one at a time in the "
        f"turns that follow.\n\n"
        f"## Candidate Overview\n{candidate_index}\n\n"
        f"## Session Instructions\n"
        f"- Analyse each candidate fully through your 4-layer protocol when it arrives.\n"
        f"- Track network surplus allocations across candidates -- if WH007 surplus is "
        f"claimed for Candidate 1, note that when Candidate 2 also targets WH007.\n"
        f"- Do NOT generate the final Restock Quote yet. Wait for the explicit quote "
        f"creation instruction at the end of this session.\n"
        f"- Respond to this briefing with a brief acknowledgement only."
    )

    messages = [{"role": "user", "content": briefing_prompt}]

    print("\n📋 Turn 0 -- Opening briefing...")
    briefing_ack = _invoke(w, endpoint_name, messages)
    messages.append({"role": "assistant", "content": briefing_ack})
    print(f"   Supervisor acknowledged: {briefing_ack[:200]}{'...' if len(briefing_ack) > 200 else ''}")

    # ══════════════════════════════════════════════════════════════════════════
    # TURNS 1..N -- Per-Candidate Analysis
    # One HTTP request per candidate. Each request resets the 290s gateway
    # clock. The accumulated message history gives the Supervisor full context
    # of all prior analyses (surplus claims, supplier rankings, etc.).
    # ══════════════════════════════════════════════════════════════════════════

    individual_reports = []  # preserved for the compression + quote turns

    for i, candidate in enumerate(candidates, 1):
        sig = candidate.get("signal_type", "STOCK_THRESHOLD")
        urg = candidate.get("initial_urgency", "CRITICAL")
        print(f"\n🔄 Turn {i}/{len(candidates)} -- Analyzing {candidate['item_id']} @ {candidate['warehouse_id']} [{sig} | {urg}]...")

        # Signal-type routing hint so the Supervisor leads with the right layer
        routing_hints = {
            "STOCK_THRESHOLD":    "Execute the full 4-layer protocol (Forecast Validation → Procurement → Manufacturing → Financial).",
            "PREDICTED_STOCKOUT": "Lead with Layer 1 (burn rate & stockout forecast), then Layer 2 (transfer vs PO to prevent stockout).",
            "BOM_CASCADE_RISK":   "Lead with Layer 3 (BOM explosion & assembly risk), then resolve component procurement in Layer 2.",
        }
        routing_hint = routing_hints.get(sig, "Execute the full 4-layer protocol.")

        surplus_reminder = (
            "\nRemember: check your analysis history above for any network surplus "
            "already allocated to previous candidates before recommending transfers."
        ) if i > 1 else ""

        candidate_prompt = (
            f"## Candidate {i}/{len(candidates)} Analysis\n\n"
            f"{routing_hint}{surplus_reminder}\n\n"
            f"```json\n{json.dumps(candidate, indent=2, default=str)}\n```"
        )

        messages.append({"role": "user", "content": candidate_prompt})
        turn_text = _invoke(w, endpoint_name, messages)
        messages.append({"role": "assistant", "content": turn_text})
        individual_reports.append(turn_text)
        print(f"   ✅ Turn {i} complete ({len(turn_text)} chars).")

    # ══════════════════════════════════════════════════════════════════════════
    # [OPTIONAL] HISTORY COMPRESSION
    # For large batches the accumulated assistant turns grow token-heavy.
    # Ask the Supervisor to compress the analyses into structured summaries,
    # then rebuild the message history around that compact exchange so the
    # quote-creation turn stays well within the context window.
    # ══════════════════════════════════════════════════════════════════════════

    if len(candidates) > COMPRESSION_THRESHOLD:
        print(f"\n🗜️  Compressing history ({len(candidates)} candidates > threshold {COMPRESSION_THRESHOLD})...")

        compression_prompt = (
            "Please produce a concise structured summary of every candidate analysis "
            "completed so far. For each candidate include:\n"
            "- Part ID, Warehouse, Signal Type, Urgency\n"
            "- Key Layer 1 findings (burn rate, anomaly score, stockout date)\n"
            "- Key Layer 2 decision (PO veto / surplus transfer / external PO + supplier)\n"
            "- Key Layer 3 finding (BOM risk or N/A)\n"
            "- Key Layer 4 figures (cost of inaction Rs, cost of overstock Rs)\n"
            "- Final recommendation (1-2 sentences)\n\n"
            "Label each block clearly as: [CANDIDATE SUMMARY: <item_id>]"
        )

        # Keep only the Turn 0 briefing exchange, replace per-candidate turns
        # with a single compressed summary exchange.
        messages_compressed = messages[:2]  # briefing user + assistant
        messages_compressed.append({"role": "user", "content": compression_prompt})
        compression_summary = _invoke(w, endpoint_name, messages_compressed)
        messages_compressed.append({"role": "assistant", "content": compression_summary})

        messages = messages_compressed
        print(f"   ✅ History compressed to {len(compression_summary)} chars.")

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL TURN -- Explicit Quote Creation
    # Passes all individual analysis reports explicitly in the prompt so the
    # Supervisor has the full per-item detail even after history compression.
    # No Genie tool calls should occur here -- synthesis only.
    # ══════════════════════════════════════════════════════════════════════════

    explicit_reports_block = "\n\n---\n\n".join(
        f"### Individual Report -- Candidate {i+1}: "
        f"{candidates[i]['item_id']} @ {candidates[i]['warehouse_id']}\n\n{report}"
        for i, report in enumerate(individual_reports)
    )

    quote_prompt = (
        f"All {len(candidates)} candidate(s) have been individually analysed. "
        f"The complete per-candidate reports are attached below for reference.\n\n"
        f"Now produce the final consolidated **Restock Intelligence Quote** for this session:\n\n"
        f"1. Order recommendations by urgency: CRITICAL → HIGH → MEDIUM → LOW.\n"
        f"2. For each candidate: state the recommended action (transfer / PO / veto), "
        f"supplier (if PO), quantity, and Rs cost of inaction vs action.\n"
        f"3. Flag any cross-candidate conflicts (surplus double-allocation, "
        f"shared-supplier PO consolidation opportunities).\n"
        f"4. Close with a prioritised action list the Production Manager must approve.\n"
        f"5. Do NOT make additional Genie tool calls -- synthesise from the reports below.\n\n"
        f"## Individual Candidate Reports\n\n{explicit_reports_block}"
    )

    messages.append({"role": "user", "content": quote_prompt})

    print(f"\n📊 Final turn -- Generating consolidated Restock Quote...")
    final_text = _invoke(w, endpoint_name, messages)
    print("\n=== Supervisor Agent -- Consolidated Restock Quote ===\n")
    print(final_text)

    dbutils.jobs.taskValues.set(key="supervisor_response", value=final_text)

    # ── Persist Quote & dispatch Teams card (unchanged) ────────────────────────

    if candidates and final_text:
        from agentic_restock.quote_persistence import persist_quote
        quote_id = persist_quote(
            candidates=candidates,
            supervisor_response_text=final_text,
            spark=spark
        )
        print(f"\nSuccessfully persisted Restock Quote '{quote_id}' into fact_restock_request and quote_metadata tables.")
        dbutils.jobs.taskValues.set(key="quote_id", value=quote_id)

        # --- Teams Adaptive Card notification ---
        from agentic_restock.integrations.teams_webhook import build_review_app_url, send_quote_card

        review_url = build_review_app_url(
            quote_id=quote_id,
            workspace_url=spark.conf.get("spark.databricks.workspaceUrl", None)
                if hasattr(spark, "conf") else None,
        )

        teams_result = send_quote_card(
            quote_id=quote_id,
            candidates=candidates,
            supervisor_summary=final_text,
            review_app_url=review_url,
            webhook_url=teams_webhook_url,
        )

        # Update quote_metadata with Teams dispatch fields
        if teams_result.get("teams_message_id"):
            spark.sql(f"""
                UPDATE gold_dev.supply_chain_analytics.quote_metadata
                SET teams_message_id = '{teams_result["teams_message_id"]}',
                    teams_sent_at = current_timestamp(),
                    databricks_preview_url = '{review_url}',
                    updated_at = current_timestamp()
                WHERE quote_id = '{quote_id}'
            """)
            print(f"quote_metadata updated with Teams message ID: {teams_result['teams_message_id']}")
