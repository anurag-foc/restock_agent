# Databricks notebook source
# MAGIC %md
# MAGIC # Invoke Supervisor Agent (stub)
# MAGIC
# MAGIC Placeholder for the real Supervisor Agent invocation — next roadmap item.
# MAGIC This task only runs when the upstream `has_candidates` condition task
# MAGIC evaluates to `true`, i.e. the coarse check found at least one low-stock
# MAGIC candidate. Replace the body of this notebook with the actual Supervisor
# MAGIC Agent call (Agent Framework / Model Serving invocation) once it exists.

# COMMAND ----------

import json

candidates_json = dbutils.jobs.taskValues.get(
    taskKey="coarse_check", key="candidates_json", default="[]", debugValue="[]"
)
candidates = json.loads(candidates_json)

print(f"TODO: invoke Supervisor Agent with {len(candidates)} candidate(s):")
for c in candidates:
    print(f"  {c['item_id']} @ {c['warehouse_id']} (suggested_reorder_qty={c['suggested_reorder_qty']})")
