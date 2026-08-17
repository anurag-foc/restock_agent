# Databricks notebook source
# MAGIC %md
# MAGIC # Lakeflow Trigger — Coarse Low-Stock Check (architecture §4.1)
# MAGIC
# MAGIC Cheap indexed join between `inventory_stock_level` and `threshold_config_table`.
# MAGIC Runs hourly. Sets two task values consumed by the job's `has_candidates`
# MAGIC branch and, when non-empty, the `invoke_supervisor` task downstream:
# MAGIC - `candidate_count` — number of item/warehouse rows at or below reorder point
# MAGIC - `candidates_json` — the candidate rows themselves

# COMMAND ----------

import sys

sys.path.append("../../src")

import json

from agentic_restock.jobs.lakeflow_trigger import build_coarse_check_query

# COMMAND ----------

dbutils.widgets.text("catalog", "", "Catalog override (optional)")
dbutils.widgets.text("schema", "", "Schema override (optional)")

catalog = dbutils.widgets.get("catalog") or None
schema = dbutils.widgets.get("schema") or None

query = build_coarse_check_query(catalog=catalog, schema=schema)
print(query)

# COMMAND ----------

candidates = [row.asDict() for row in spark.sql(query).collect()]

print(f"Coarse check found {len(candidates)} low-stock candidate(s).")
for c in candidates:
    print(f"  {c['item_id']} @ {c['warehouse_id']}: {c['current_stock_qty']} <= {c['reorder_point_qty']}")

# COMMAND ----------

dbutils.jobs.taskValues.set(key="candidate_count", value=len(candidates))
dbutils.jobs.taskValues.set(key="candidates_json", value=json.dumps(candidates, default=str))
