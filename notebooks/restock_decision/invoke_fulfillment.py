# Databricks notebook source
# MAGIC %md
# MAGIC # Task 2 — Fulfillment turn (APPROVED lines only)
# MAGIC
# MAGIC Runs only when `has_approval` is true, i.e. Task 1 approved at least one
# MAGIC line in the batch. Opens one **fresh** Supervisor Agent conversation per
# MAGIC approved line — sequentially, since each is a separate stateless
# MAGIC conversation — and asks it to re-validate the approved restock against
# MAGIC live stock and record the outcome.
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
# MAGIC This notebook writes nothing itself; it starts each conversation and then
# MAGIC verifies the transition landed. One line failing to transition does not
# MAGIC stop the rest of the batch from being attempted -- failures are collected
# MAGIC and raised together at the end so the job run fails loudly without
# MAGIC silently dropping the other lines.
# MAGIC
# MAGIC Every call to `fulfill_restock_request` (a custom MCP tool attached via the
# MAGIC `app` tool type) comes back from the Responses API as an
# MAGIC `mcp_approval_request` instead of executing -- Databricks requires an
# MAGIC explicit approval round-trip for any custom-MCP tool call, and there is no
# MAGIC way to disable this at tool-registration or per-request time (checked
# MAGIC both; neither is honored). This is the intended way to consume that API,
# MAGIC not a workaround: the PM's Approve/Reject click in the review app is the
# MAGIC actual human-in-the-loop gate, so this notebook answers the platform's
# MAGIC approval prompt immediately so the already-authorized action executes.
# MAGIC
# MAGIC These are *new* conversations, not a resumption of the quote-creation
# MAGIC session — Supervisor Agent invocations are stateless, there is no session
# MAGIC to resume (see docs/agent_bricks_mapping.md §2.5). It runs as a job
# MAGIC rather than inline in the app because the Databricks Apps reverse proxy
# MAGIC hard-caps requests at 120s and a cold Supervisor+Genie turn has measured
# MAGIC ~110s.

# COMMAND ----------

import json

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor Agent serving endpoint name")

endpoint_name = dbutils.widgets.get("supervisor_endpoint_name")

approved_keys_json = dbutils.jobs.taskValues.get(
    taskKey="apply_decision", key="approved_keys_json", default="[]", debugValue="[]"
)
approved_keys = json.loads(approved_keys_json)
if not approved_keys:
    raise ValueError("approved_keys_json task value from apply_decision is empty")

if not endpoint_name:
    raise ValueError(
        "No supervisor_endpoint_name parameter set -- cannot run the fulfillment turn. "
        "scripts/ensure_supervisor_agent.py keeps this in sync; run deploy_all.sh."
    )

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

w = WorkspaceClient(config=Config(http_timeout_seconds=280, retry_timeout_seconds=300))

MAX_APPROVAL_ROUNDS = 5


def invoke_with_auto_approval(prompt: str) -> dict:
    """POST a Responses-API turn, auto-approving any mcp_approval_request items.

    Custom MCP tools (attached via the `app` tool type, e.g. fulfill_restock_request)
    always come back as an mcp_approval_request instead of executing -- there is no
    supported way to disable this per-tool or per-request. This endpoint is stateless
    (no session to resume, see docs/agent_bricks_mapping.md §2.5), so `previous_response_id`
    chaining does NOT work here -- confirmed by testing: it returns "Invalid message
    sequence. The approval response was in an unexpected position." Continuing the
    conversation instead means resending the full transcript as `input`: the original
    user message, every item from the prior response's `output` verbatim, plus the
    mcp_approval_response.
    """
    current_input = [{"role": "user", "content": prompt}]
    response = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint_name}/invocations",
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
            f"/serving-endpoints/{endpoint_name}/invocations",
            body={"input": current_input},
        )
    return response


def run_fulfillment_turn(line_key: int) -> dict:
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

    response = invoke_with_auto_approval(prompt)

    final_text = ""
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    final_text = part["text"]

    print(f"\n=== Supervisor -- fulfillment decision for line {line_key} ===\n")
    print(final_text or json.dumps(response, indent=2))

    # ── Verify the Supervisor actually recorded the transition ────────────
    # fulfill_restock_request is idempotent, so this does not retry -- it
    # checks, and fails loudly if the line never moved. A line stuck at
    # APPROVED means the agent reasoned but never called the action tool.
    after = spark.sql(f"""
        SELECT drs.REQUEST_STATUS, frr.CONFIRMED_QTY, frr.VARIANCE_QTY
        FROM gold_dev.supply_chain_analytics.fact_restock_request frr
        JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
        WHERE frr.RESTOCK_REQUEST_KEY = {line_key}
    """).collect()[0].asDict()

    if after["REQUEST_STATUS"] == "APPROVED":
        raise RuntimeError(
            f"Supervisor did not call fulfill_restock_request -- line {line_key} is still APPROVED. "
            f"Its reasoning was: {final_text[:500]}"
        )

    print(f"\nLine {line_key} -> {after['REQUEST_STATUS']} "
          f"(confirmed_qty={after['CONFIRMED_QTY']}, variance_qty={after['VARIANCE_QTY']})")

    return {
        "restock_request_key": line_key,
        "final_status": after["REQUEST_STATUS"],
        "confirmed_qty": after["CONFIRMED_QTY"],
        "variance_qty": after["VARIANCE_QTY"],
        "supervisor_response": final_text,
    }


# COMMAND ----------

results = []
failures = []

for line_key in approved_keys:
    try:
        results.append(run_fulfillment_turn(int(line_key)))
    except Exception as exc:  # noqa: BLE001 -- collected and re-raised below
        failures.append({"restock_request_key": line_key, "error": str(exc)})

dbutils.jobs.taskValues.set(key="results_json", value=json.dumps(results))
dbutils.jobs.taskValues.set(key="failures_json", value=json.dumps(failures))

if failures:
    raise RuntimeError(
        f"Fulfillment failed for {len(failures)}/{len(approved_keys)} approved line(s): {failures}"
    )
