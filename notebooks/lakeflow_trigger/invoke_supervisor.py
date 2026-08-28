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

# COMMAND ----------

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor Agent serving endpoint name")

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

candidates_json = dbutils.jobs.taskValues.get(
    taskKey="coarse_check", key="candidates_json", default="[]", debugValue="[]"
)
candidates = json.loads(candidates_json)
endpoint_name = dbutils.widgets.get("supervisor_endpoint_name")

print(f"{len(candidates)} candidate(s) from the coarse check:")
for c in candidates:
    print(
        f"  - {c['item_id']} @ {c['warehouse_id']}: {c['current_stock_qty']} on hand "
        f"(reorder point {c['reorder_point_qty']})"
    )

# COMMAND ----------

if not endpoint_name:
    print(
        "No supervisor_endpoint_name job parameter set -- skipping the live call. "
        "Set it to the Supervisor Agent's endpoint_name (e.g. mas-<id>-endpoint, "
        "printed by scripts/create_supervisor_agent.py) to invoke it for real."
    )
else:
    prompt = (
        "The Lakeflow multi-signal agentic scanner flagged the following supply chain candidates. "
        "Each candidate includes its specific signal_type (STOCK_THRESHOLD, PREDICTED_STOCKOUT, or BOM_CASCADE_RISK). "
        "Route each candidate through your 4-layer reasoning protocol (Forecast Validation → Procurement Intelligence → Manufacturing Constraints → Financial Framing). "
        "Apply the restock veto, surface lateral transfer vs PO options, explode BOM components if applicable, "
        "and produce a prioritized intelligence quote (CRITICAL first):\n\n" + json.dumps(candidates, default=str)
    )

    # Ambient auth inside a Databricks job/notebook -- no explicit host/token needed.
    # A cold Supervisor Agent + Genie Agent + several UC function calls can take
    # 1-2 minutes end to end, so give it generous HTTP + retry timeouts via a
    # Config object passed to the constructor. WorkspaceClient() itself doesn't
    # accept http_timeout_seconds/retry_timeout_seconds as kwargs, and setting
    # w.config.retry_timeout_seconds AFTER construction has no effect -- the
    # ApiClient reads these off Config once, at construction time, and
    # otherwise falls back to the SDK's default 5-minute retry deadline.
    w = WorkspaceClient(config=Config(http_timeout_seconds=600, retry_timeout_seconds=900))
    response = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint_name}/invocations",
        body={"input": [{"role": "user", "content": prompt}]},
    )

    # Pull out the final assistant message text (the OpenAI Responses API
    # returns a list of output items -- messages, function_calls, and
    # function_call_outputs interleaved -- so take the last message's text).
    final_text = ""
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    final_text = part["text"]

    print("Supervisor Agent response:\n")
    print(final_text or json.dumps(response, indent=2))

    dbutils.jobs.taskValues.set(key="supervisor_response", value=final_text)

    # Persist Quote line items & metadata to Delta tables
    if candidates and final_text:
        from agentic_restock.quote_persistence import persist_quote
        quote_id = persist_quote(
            candidates=candidates,
            supervisor_response_text=final_text,
            spark=spark
        )
        print(f"\nSuccessfully persisted Restock Quote '{quote_id}' into fact_restock_request and quote_metadata tables.")
        dbutils.jobs.taskValues.set(key="quote_id", value=quote_id)
