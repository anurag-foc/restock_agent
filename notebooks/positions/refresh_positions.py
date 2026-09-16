# Databricks notebook source
# MAGIC %md
# MAGIC # Refresh Positions
# MAGIC
# MAGIC Builds the corrected-measures layer: `part_position` and
# MAGIC `supplier_performance`. Successor to `refresh_signal_board`, and the
# MAGIC difference is what it does *not* do.
# MAGIC
# MAGIC The board mixed measurement with judgement -- burn, lead time, transfer
# MAGIC options, cascade exposure and a ranking hint in one wide row per
# MAGIC (part, warehouse). That is how six of the eight intelligence nuances
# MAGIC ended up as columns which could never be *the reason* something was
# MAGIC flagged (docs/intelligence_layer_design.md §1).
# MAGIC
# MAGIC These two tables hold only corrected measurement: what is on hand, how
# MAGIC fast it is really moving, how long the supplier really takes, and how
# MAGIC well each of those is known. No exposure, no ranking, no fix -- those
# MAGIC belong to the scanners, each running at its own natural grain.
# MAGIC
# MAGIC All the logic lives in `agentic_restock.jobs.positions` and
# MAGIC `agentic_restock.estimators`, which are pure functions with no Spark
# MAGIC import, so Phase 2's accuracy gate runs as a local test rather than a
# MAGIC cluster job (tests/test_ground_truth_recovery.py). This notebook is only
# MAGIC the Spark glue: read, apply, write.

# COMMAND ----------

# MAGIC %md
# MAGIC `statsforecast` backs the second consumption engine (`consumption_model =
# MAGIC 'statsforecast'`). It is installed unconditionally because `%pip` restarts the
# MAGIC Python process and so cannot be made conditional on a setting read later in the
# MAGIC run -- the alternative is an option the admin panel offers and the job then fails
# MAGIC on, which is worse than the install.
# MAGIC
# MAGIC Cost is roughly half a minute on a cold environment, on a job that runs twice a
# MAGIC day. There is no wheel build in this bundle (the source is put on `sys.path`
# MAGIC below), so `pyproject.toml` dependencies never reach the cluster and this line is
# MAGIC the only mechanism available. Delete it and the default `automatic` engine is
# MAGIC unaffected.

# COMMAND ----------

# MAGIC %pip install statsforecast==2.1.1

# COMMAND ----------

import sys
from datetime import date

sys.path.append("../../src")

from agentic_restock import settings as st
from agentic_restock.jobs import positions

# COMMAND ----------

dbutils.widgets.text("gold_catalog", "", "Data Engineering catalog override (optional, default gold_dev)")
dbutils.widgets.text("dim_schema", "", "Dimension schema override (optional, default dim)")
dbutils.widgets.text("facts_schema", "", "Facts schema override (optional, default supply_chain_analytics)")
dbutils.widgets.text("app_catalog", "", "Application catalog override (optional)")
dbutils.widgets.text("app_schema", "", "Application schema override (optional)")
dbutils.widgets.text("as_of", "", "Override today's date as yyyy-mm-dd (optional; for backfills)")

gold_catalog = dbutils.widgets.get("gold_catalog") or None
dim_schema = dbutils.widgets.get("dim_schema") or None
facts_schema = dbutils.widgets.get("facts_schema") or None
app_catalog = dbutils.widgets.get("app_catalog") or None
app_schema = dbutils.widgets.get("app_schema") or None

as_of_text = dbutils.widgets.get("as_of")
as_of = date.fromisoformat(as_of_text) if as_of_text else date.today()
print(f"as_of = {as_of}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC
# MAGIC Which consumption engine to use. An empty table resolves to `automatic` —
# MAGIC the behaviour that shipped before the settings layer existed — so this is a
# MAGIC no-op until a client deliberately changes something. Lead time has no
# MAGIC engine choice: E2 estimates a distribution over observed deliveries, not a
# MAGIC forecast, so there is nothing to select between.
# MAGIC
# MAGIC Created here as well as in `run_intelligence` because either job can run
# MAGIC first, and `CREATE TABLE IF NOT EXISTS` makes the second one free.

# COMMAND ----------

spark.sql(st.build_settings_table_ddl(app_catalog, app_schema))
settings = st.resolve(
    spark.sql(st.build_settings_read_query(app_catalog, app_schema))
    .toPandas()
    .to_dict("records")
)
print(f"consumption model: {settings.consumption_model}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read
# MAGIC
# MAGIC Four reads. The issue history is the big one (~200K rows over three
# MAGIC years) and it has to be that long: the seasonal index needs 2+ years, and
# MAGIC reading less silently disables the seasonal branch rather than failing.

# COMMAND ----------

issue_rows = spark.sql(
    positions.build_issue_history_query(gold_catalog, dim_schema, facts_schema)
).toPandas()

position_rows = spark.sql(
    positions.build_current_position_query(gold_catalog, dim_schema, facts_schema)
).toPandas()

delivery_rows = spark.sql(
    positions.build_delivery_history_query(gold_catalog, dim_schema, facts_schema)
).toPandas()

contract_rows = spark.sql(positions.build_contract_query(app_catalog, app_schema)).toPandas()

# Cascade inputs. The plan is read FORWARD of today over the 14-day frozen
# window -- that is what makes a cascade "what are we about to fail to build"
# rather than "what did we fail to build". At a 60-day horizon a plant at line
# rate outruns every component buffer, so every cascade finding becomes enormous
# and swamps the other seven signal types.
plan_rows = spark.sql(
    positions.build_production_plan_query(gold_catalog, dim_schema, facts_schema)
).toPandas()

model_bom_rows = spark.sql(positions.build_model_bom_query(app_catalog, app_schema)).toPandas()
bom_rows = spark.sql(positions.build_bom_query(app_catalog, app_schema)).toPandas()

print(
    f"issues {len(issue_rows):,} | pairs {len(position_rows):,} | "
    f"deliveries {len(delivery_rows):,} | contracts {len(contract_rows):,}"
)
print(
    f"plan models {len(plan_rows):,} | model-bom {len(model_bom_rows):,} | "
    f"bom edges {len(bom_rows):,}"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Estimate
# MAGIC
# MAGIC Collected to the driver deliberately. At this working set (~280 pairs,
# MAGIC ~200K issue rows) the estimators run in about a second and the code stays
# MAGIC identical to what the local gate tests. At production volume -- hundreds
# MAGIC of thousands of pairs -- this is the line to change: `densify_issues` and
# MAGIC the per-pair `estimate_burn` call are already per-group pure functions,
# MAGIC so they move to `groupBy(...).applyInPandas(...)` without touching the
# MAGIC estimator itself. Doing that now would buy nothing and cost the local
# MAGIC gate.

# COMMAND ----------

supplier_performance = positions.build_supplier_performance(
    delivery_rows, contract_rows, as_of=as_of
)
part_position = positions.build_part_position(
    position_rows, issue_rows, supplier_performance, as_of=as_of, settings=settings
)

print(f"supplier_performance {len(supplier_performance):,} rows")
print(f"part_position {len(part_position):,} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Cascade, then exposure
# MAGIC
# MAGIC Order matters and is not interchangeable. `build_parent_cascades`
# MAGIC computes value at risk **at the parent**, counted once, and returns the
# MAGIC binding child set; `attach_exposure` then reads that back down onto the
# MAGIC children. Accumulating upward from each child instead is what made three
# MAGIC components blocking one engine each claim the engine's full value.
# MAGIC
# MAGIC `attach_exposure` is what puts `EXPOSURE`, `P_STOCKOUT`, `CONSEQUENCE`
# MAGIC and `THREATENED_PARENT_PART_ID` on `part_position`. Every scanner filters
# MAGIC on the first two, so a `part_position` written without this step is a
# MAGIC table every detector reads successfully and finds nothing in.

# COMMAND ----------

parent_cascades = positions.build_parent_cascades(
    part_position, plan_rows, model_bom_rows, bom_rows
)
part_position = positions.attach_exposure(part_position, parent_cascades, bom_rows)

print(f"parent_cascade {len(parent_cascades):,} rows")
if not parent_cascades.empty:
    print(f"  total value at risk {parent_cascades['VALUE_AT_RISK'].sum():,.0f}")
print(f"part_position now {part_position.shape[1]} columns (exposure attached)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write
# MAGIC
# MAGIC `CREATE OR REPLACE` -- both tables are derived and rebuilt wholesale, so
# MAGIC there is no incremental state to preserve and no partial-write state to
# MAGIC reason about.

# COMMAND ----------

app_catalog_name = app_catalog or "gold_dev"
app_schema_name = app_schema or "supply_chain_analytics"

for frame, table in (
    (part_position, positions.PART_POSITION_TABLE),
    (supplier_performance, positions.SUPPLIER_PERFORMANCE_TABLE),
    (parent_cascades, positions.PARENT_CASCADE_TABLE),
):
    if frame.empty:
        # An empty cascade frame is legitimate (nothing planned is blocked) but
        # createDataFrame cannot infer a schema from it, and skipping the write
        # silently leaves the previous run's table in place. Say so.
        print(f"SKIPPED {table}: no rows to write")
        continue
    fqn = f"{app_catalog_name}.{app_schema_name}.{table}"
    spark.createDataFrame(frame).write.mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(fqn)
    print(f"wrote {len(frame):,} rows -> {fqn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify
# MAGIC
# MAGIC Fail loudly rather than leaving a half-built layer for the scanners to
# MAGIC read. An empty or partial `part_position` would make every downstream
# MAGIC detector quietly find nothing, which looks exactly like a quiet day.

# COMMAND ----------

position_fqn = f"{app_catalog_name}.{app_schema_name}.{positions.PART_POSITION_TABLE}"
written = spark.sql(f"SELECT COUNT(*) c FROM {position_fqn}").collect()[0]["c"]

if written != len(part_position):
    raise AssertionError(f"{position_fqn}: wrote {len(part_position)} rows, read back {written}")

if written == 0:
    raise AssertionError(f"{position_fqn} is empty -- every scanner would find nothing")

unmeasured = spark.sql(
    f"SELECT COUNT(*) c FROM {position_fqn} WHERE FORWARD_BURN IS NULL OR MU_LEAD_DAYS IS NULL"
).collect()[0]["c"]
print(f"{written:,} rows; {unmeasured:,} missing a burn or lead-time estimate")

# The first live run failed here in the worst available way: this task reported
# SUCCESS having written a part_position with no exposure columns and no
# parent_cascade table at all, and the failure only surfaced one task later as a
# TABLE_OR_VIEW_NOT_FOUND. Verifying only the table this notebook happened to
# name is how a half-built layer gets declared complete, so all three are
# checked, and the columns the scanners actually filter on are checked by name.
present = {
    r["col_name"]
    for r in spark.sql(f"SHOW COLUMNS IN {position_fqn}").collect()
}
required = {"EXPOSURE", "P_STOCKOUT", "CONSEQUENCE", "THREATENED_PARENT_PART_ID"}
missing = {c for c in required if c not in present and c.lower() not in present}
if missing:
    raise AssertionError(
        f"{position_fqn} is missing {sorted(missing)} -- attach_exposure did not run. "
        "Every scanner filters on P_STOCKOUT and EXPOSURE, so this table would read "
        "fine and yield nothing."
    )

cascade_fqn = f"{app_catalog_name}.{app_schema_name}.{positions.PARENT_CASCADE_TABLE}"
supplier_fqn = f"{app_catalog_name}.{app_schema_name}.{positions.SUPPLIER_PERFORMANCE_TABLE}"
for fqn, frame in ((cascade_fqn, parent_cascades), (supplier_fqn, supplier_performance)):
    if frame.empty:
        print(f"{fqn}: nothing written this run (source frame empty)")
        continue
    count = spark.sql(f"SELECT COUNT(*) c FROM {fqn}").collect()[0]["c"]
    if count != len(frame):
        raise AssertionError(f"{fqn}: wrote {len(frame)} rows, read back {count}")
    print(f"{fqn}: {count:,} rows")

dbutils.notebook.exit(
    f"part_position={written} parent_cascade={len(parent_cascades)} "
    f"supplier_performance={len(supplier_performance)}"
)
