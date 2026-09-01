# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy Phase-1 Priority Functions
# MAGIC
# MAGIC (Re)registers the seven phase-1 intelligence functions
# MAGIC (docs/market_evidence_phase1.md §7) in
# MAGIC `gold_dev.supply_chain_analytics`, all reading
# MAGIC `inventory_signal_board` rather than recomputing anything the board
# MAGIC already carries. `CREATE OR REPLACE`, idempotent, safe to rerun.
# MAGIC
# MAGIC Requires `refresh_signal_board` to have run at least once first, so the
# MAGIC board these functions read from actually exists.
# MAGIC
# MAGIC These are additive to the existing `deploy_uc_functions` job's sixteen
# MAGIC functions in `deep_analysis_functions.ipynb` -- that notebook is left
# MAGIC untouched by this change. Several of those sixteen are now redundant
# MAGIC with board columns (see priority_functions.py module docstring for
# MAGIC which), but retiring them is a separate decision: `fulfillment_guardrail`'s
# MAGIC Genie Space and the restock_decision job's fulfillment path were not
# MAGIC audited for dependence on them as part of this change, so they stay
# MAGIC until that's checked.

# COMMAND ----------

import sys

sys.path.append("../../src")

from agentic_restock.jobs.priority_functions import FUNCTION_NAMES, build_function_statements

# COMMAND ----------

dbutils.widgets.text("app_catalog", "", "Catalog override (optional, default gold_dev)")
dbutils.widgets.text("app_schema", "", "Schema override (optional, default supply_chain_analytics)")

app_catalog = dbutils.widgets.get("app_catalog") or None
app_schema = dbutils.widgets.get("app_schema") or None

statements = build_function_statements(app_catalog=app_catalog, app_schema=app_schema)

for name, statement in zip(FUNCTION_NAMES, statements):
    print(f"Deploying {name}...")
    spark.sql(statement)
    print(f"  done.")

print(f"\nDeployed {len(statements)} function(s): {', '.join(FUNCTION_NAMES)}")
