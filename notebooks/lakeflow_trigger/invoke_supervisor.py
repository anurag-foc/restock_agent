# Databricks notebook source
# MAGIC %md
# MAGIC # Invoke Supervisor Agent
# MAGIC
# MAGIC Runs only when `has_candidates` is true, i.e. the §4.1 coarse check found
# MAGIC at least one item below its reorder point. Hands the candidate list off to
# MAGIC the real Supervisor Agent endpoint (see `scripts/create_supervisor_agent.py`
# MAGIC and `docs/agent_bricks_mapping.md`) and lets it drive everything from there:
# MAGIC Genie deep-analysis (via the §4.2 Unity Catalog functions), the veto
# MAGIC decision, and — once built — writing `open_request` and sending the Teams
# MAGIC Adaptive Card.
# MAGIC
# MAGIC The deep analysis itself (consumption trend, stockout forecast, urgency,
# MAGIC veto, quote) is **not** done here. It lives in the Unity Catalog functions
# MAGIC in `ab_training.agentic_restock` that the Supervisor Agent and Genie Agent
# MAGIC call as tools.

# COMMAND ----------

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor Agent serving endpoint name")

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient

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
        "The Lakeflow trigger job's coarse check (architecture §4.1) flagged the "
        "following item/warehouse candidates as being at or below their reorder point. "
        "For each, apply the restock veto, compute urgency and the suggested reorder "
        "quantity, and produce a single prioritized summary (CRITICAL first) suitable "
        "for a Teams notification:\n\n" + json.dumps(candidates, default=str)
    )

    # Ambient auth inside a Databricks job/notebook -- no explicit host/token needed.
    # A cold Supervisor Agent + Genie Agent + several UC function calls can take
    # 1-2 minutes end to end, so give it generous HTTP + retry timeouts.
    w = WorkspaceClient()
    w.config.http_timeout_seconds = 600
    w.config.retry_timeout_seconds = 900
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
