# Databricks notebook source
# MAGIC %md
# MAGIC # Refresh Signal Board
# MAGIC
# MAGIC Replaces the §4.1 coarse check. Rebuilds
# MAGIC `gold_dev.supply_chain_analytics.inventory_signal_board` -- one row per
# MAGIC (part, warehouse), all seven phase-1 intelligence nuances computed
# MAGIC set-wise (docs/market_evidence_phase1.md §7) -- then reports how many
# MAGIC part/warehouse pairs cleared `rank_priority_actions`' materiality floor.
# MAGIC
# MAGIC This notebook does not decide what matters. It rebuilds the board and
# MAGIC counts; `invoke_supervisor.py` reads that count to decide whether to open
# MAGIC a Supervisor conversation at all, and the Supervisor itself calls
# MAGIC `rank_priority_actions` fresh through Genie rather than being handed a
# MAGIC pre-computed list -- the same reason the old coarse check never passed
# MAGIC row content to the Supervisor beyond a count.

# COMMAND ----------

import sys

sys.path.append("../../src")

from agentic_restock.jobs.signal_board import build_signal_board_query

# COMMAND ----------

dbutils.widgets.text("gold_catalog", "", "Data Engineering catalog override (optional, default gold_dev)")
dbutils.widgets.text("dim_schema", "", "Dimension schema override (optional, default dim)")
dbutils.widgets.text("facts_schema", "", "Facts schema override (optional, default supply_chain_analytics)")

gold_catalog = dbutils.widgets.get("gold_catalog") or None
dim_schema = dbutils.widgets.get("dim_schema") or None
facts_schema = dbutils.widgets.get("facts_schema") or None

query = build_signal_board_query(gold_catalog=gold_catalog, dim_schema=dim_schema, facts_schema=facts_schema)
spark.sql(query)

board = f"{gold_catalog or 'gold_dev'}.{facts_schema or 'supply_chain_analytics'}.inventory_signal_board"
row_count = spark.sql(f"SELECT COUNT(*) c FROM {board}").collect()[0]["c"]
print(f"Signal board rebuilt: {row_count:,} part/warehouse rows.")

# This notebook deliberately does NOT call rank_priority_actions to report a
# candidate count, even though that would be a useful thing to see in the job
# UI. Spark validates a SQL function's body at CREATE time, so the seven
# phase-1 functions can only be created once this board exists -- which makes
# deploy_uc_functions run refresh_signal_board first. If this notebook then
# called one of those functions, a fresh workspace would deadlock: the board
# refresh would fail on a missing function, so the function that would have
# fixed it never gets deployed. invoke_supervisor.py counts instead; it runs
# only after the functions are known to exist.
