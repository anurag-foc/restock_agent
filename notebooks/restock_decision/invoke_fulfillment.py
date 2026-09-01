# Databricks notebook source
# MAGIC %md
# MAGIC # Task 2 — Fulfillment turn (APPROVED lines only)
# MAGIC
# MAGIC Runs only when `has_approval` is true, i.e. Task 1 actually moved a line
# MAGIC to APPROVED. Opens a **fresh** Supervisor Agent conversation and asks it
# MAGIC to re-validate the approved restock against live stock and record the
# MAGIC outcome.
# MAGIC
# MAGIC The Supervisor does both halves itself, through its own tools:
# MAGIC   - `fulfillment_guardrail` (Genie Space, read-only) — a guardrail verdict
# MAGIC     only: PROCEED or NEEDS_REVIEW. It catches a request that sat
# MAGIC     PENDING_APPROVAL long enough that the stock situation already changed
# MAGIC     before it was approved. It never proposes a quantity.
# MAGIC   - `fulfill_restock_request` (MCP action tool) — computes CONFIRMED_QTY /
# MAGIC     VARIANCE_QTY itself from live data and moves the line to FULFILLING
# MAGIC     (proceed) or NEEDS_REVIEW (flagged), based on the Supervisor's verdict.
# MAGIC
# MAGIC This notebook writes nothing itself; it starts the conversation and then
# MAGIC verifies the transition landed.
# MAGIC
# MAGIC This is a *new* conversation, not a resumption of the quote-creation
# MAGIC session — Supervisor Agent invocations are stateless, there is no session
# MAGIC to resume (see docs/agent_bricks_mapping.md §2.5). It runs as a job
# MAGIC rather than inline in the app because the Databricks Apps reverse proxy
# MAGIC hard-caps requests at 120s and a cold Supervisor+Genie turn has measured
# MAGIC ~110s.

# COMMAND ----------

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor Agent serving endpoint name")

endpoint_name = dbutils.widgets.get("supervisor_endpoint_name")

restock_request_key = dbutils.jobs.taskValues.get(
    taskKey="apply_decision", key="restock_request_key", default="", debugValue=""
)
if not restock_request_key:
    raise ValueError("restock_request_key task value missing from apply_decision")

line_key = int(restock_request_key)

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

line = spark.sql(f"""
    SELECT
        frr.RESTOCK_REQUEST_KEY,
        frr.QUOTE_ID,
        dp.PART_ID,
        dw.WAREHOUSE_ID,
        drs.URGENCY_LEVEL,
        drs.REQUEST_STATUS,
        frr.REQUESTED_QTY,
        frr.CURRENT_STOCK_QTY AS QUOTE_TIME_STOCK_QTY
    FROM gold_dev.supply_chain_analytics.fact_restock_request frr
    JOIN gold_dev.dim.dim_part dp ON frr.PART_KEY = dp.PART_KEY AND dp.IS_CURRENT = true
    JOIN gold_dev.dim.dim_warehouse dw ON frr.WAREHOUSE_KEY = dw.WAREHOUSE_KEY
    JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
    WHERE frr.RESTOCK_REQUEST_KEY = {line_key}
""").collect()[0].asDict()

print(f"Fulfillment turn for line {line_key}: {line['PART_ID']} @ {line['WAREHOUSE_ID']} "
      f"({line['URGENCY_LEVEL']}), approved qty {line['REQUESTED_QTY']}, "
      f"quote-time stock {line['QUOTE_TIME_STOCK_QTY']}, status {line['REQUEST_STATUS']}")

# COMMAND ----------

if not endpoint_name:
    raise ValueError(
        "No supervisor_endpoint_name parameter set -- cannot run the fulfillment turn. "
        "scripts/ensure_supervisor_agent.py keeps this in sync; run deploy_all.sh."
    )

w = WorkspaceClient(config=Config(http_timeout_seconds=280, retry_timeout_seconds=300))

prompt = (
    f"FULFILLMENT TURN — a Production Manager has already APPROVED this restock line. "
    f"Do not create a quote and do not call persist_quote or send_human_review.\n\n"
    f"Approved line:\n"
    f"- restock_request_key: {line_key}\n"
    f"- part: {line['PART_ID']} at warehouse {line['WAREHOUSE_ID']}\n"
    f"- urgency: {line['URGENCY_LEVEL']}\n"
    f"- approved quantity: {line['REQUESTED_QTY']}\n"
    f"- stock on hand when the quote was written: {line['QUOTE_TIME_STOCK_QTY']}\n\n"
    f"Steps:\n"
    f"1. Ask the Fulfillment Guardrail for a PROCEED or NEEDS_REVIEW verdict on this line. It will "
    f"not give you a quantity -- do not ask for one, that is computed elsewhere.\n"
    f"2. Call `fulfill_restock_request` with restock_request_key={line_key}, `proceed` set to "
    f"true or false matching the verdict, and `note` set to its short reason. Do not pass a "
    f"quantity.\n"
    f"3. Reply with a two-sentence summary of the verdict and why."
)

response = w.api_client.do(
    "POST",
    f"/serving-endpoints/{endpoint_name}/invocations",
    body={"input": [{"role": "user", "content": prompt}]},
)

final_text = ""
for item in response.get("output", []):
    if item.get("type") == "message":
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                final_text = part["text"]

print("\n=== Supervisor -- fulfillment decision ===\n")
print(final_text or json.dumps(response, indent=2))

# COMMAND ----------

# ── Verify the Supervisor actually recorded the transition ────────────────────
# fulfill_restock_request is idempotent, so this does not retry -- it checks,
# and fails loudly if the line never moved. A line stuck at APPROVED means the
# agent reasoned but never called the action tool.

after = spark.sql(f"""
    SELECT drs.REQUEST_STATUS, frr.CONFIRMED_QTY, frr.VARIANCE_QTY
    FROM gold_dev.supply_chain_analytics.fact_restock_request frr
    JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
    WHERE frr.RESTOCK_REQUEST_KEY = {line_key}
""").collect()[0].asDict()

dbutils.jobs.taskValues.set(key="final_status", value=after["REQUEST_STATUS"])
dbutils.jobs.taskValues.set(key="supervisor_response", value=final_text)

if after["REQUEST_STATUS"] == "APPROVED":
    raise RuntimeError(
        f"Supervisor did not call fulfill_restock_request -- line {line_key} is still APPROVED. "
        f"Its reasoning was: {final_text[:500]}"
    )

print(f"\nLine {line_key} -> {after['REQUEST_STATUS']} "
      f"(confirmed_qty={after['CONFIRMED_QTY']}, variance_qty={after['VARIANCE_QTY']})")
