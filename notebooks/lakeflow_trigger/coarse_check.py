# Databricks notebook source
# MAGIC %md
# MAGIC # Lakeflow Trigger — Coarse Low-Stock Check (architecture §4.1)
# MAGIC
# MAGIC Cheap indexed join between the latest `fact_inventory_snapshot` row per
# MAGIC part/warehouse (Data Engineering's `gold_dev` star schema) and its
# MAGIC `dim_part`/`dim_warehouse` dimensions. Runs hourly. Sets two task values
# MAGIC consumed by the job's `has_candidates` branch and, when non-empty, the
# MAGIC `invoke_supervisor` task downstream:
# MAGIC - `candidate_count` — number of item/warehouse rows at or below reorder point
# MAGIC - `candidates_json` — the candidate rows themselves

# COMMAND ----------

import sys

sys.path.append("../../src")

import json

from agentic_restock.jobs.lakeflow_trigger import build_coarse_check_query

# COMMAND ----------

dbutils.widgets.text("gold_catalog", "", "Data Engineering catalog override (optional, default gold_dev)")
dbutils.widgets.text("dim_schema", "", "Dimension schema override (optional, default dim)")
dbutils.widgets.text("facts_schema", "", "Facts schema override (optional, default supply_chain_analytics)")

gold_catalog = dbutils.widgets.get("gold_catalog") or None
dim_schema = dbutils.widgets.get("dim_schema") or None
facts_schema = dbutils.widgets.get("facts_schema") or None

query = build_coarse_check_query(gold_catalog=gold_catalog, dim_schema=dim_schema, facts_schema=facts_schema)
print(query)

# COMMAND ----------

candidates = [row.asDict() for row in spark.sql(query).collect()]

print(f"Coarse check found {len(candidates)} low-stock candidate(s).")
for c in candidates:
    print(f"  {c['item_id']} @ {c['warehouse_id']}: {c['current_stock_qty']} <= {c['reorder_point_qty']}")

# COMMAND ----------

dbutils.jobs.taskValues.set(key="candidate_count", value=len(candidates))
dbutils.jobs.taskValues.set(key="candidates_json", value=json.dumps(candidates, default=str))
