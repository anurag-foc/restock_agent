# Databricks notebook source
# MAGIC %md
# MAGIC # Run Simulation
# MAGIC
# MAGIC Scores the pipeline against a generated world and writes one `sim_run`
# MAGIC row per engine, plus a `sim_run_pair` row per (part, warehouse).
# MAGIC
# MAGIC **This notebook reads no catalog and writes no fact table.** The
# MAGIC generator, the estimators, the scanners and the scorer are all pure
# MAGIC functions over pandas, so the whole run happens in memory. The only
# MAGIC writes are the two result tables. That is deliberate: `gold_dev_analytics`
# MAGIC is what the live pipeline reads, and a simulation that touched it would
# MAGIC make the product's own output depend on how many simulations somebody
# MAGIC ran that morning.
# MAGIC
# MAGIC The figures are **simulated, never measured savings** — see
# MAGIC `docs/simulation_feature_design.md` §2.2. Every surface that shows
# MAGIC `NET_VALUE` has to say so.

# COMMAND ----------

# MAGIC %pip install statsforecast==2.1.1

# COMMAND ----------

import json
import sys
import uuid

sys.path.append("../../src")

from agentic_restock import settings as st
from agentic_restock.simulation import persistence, run

# COMMAND ----------

dbutils.widgets.text("app_catalog", "", "Application catalog override (optional)")
dbutils.widgets.text("app_schema", "", "Application schema override (optional)")
dbutils.widgets.text("engines", "automatic,statsforecast", "Engines to compare, comma separated")
dbutils.widgets.text("budget", "", "Output budget override (optional; default from settings)")
dbutils.widgets.text("label", "", "What to call this comparison")

app_catalog = dbutils.widgets.get("app_catalog") or None
app_schema = dbutils.widgets.get("app_schema") or None
label = dbutils.widgets.get("label") or "Engine comparison"

engines = [e.strip() for e in dbutils.widgets.get("engines").split(",") if e.strip()]
budget_text = dbutils.widgets.get("budget")
budget = int(budget_text) if budget_text else None

print(f"engines = {engines} | budget = {budget or 'from settings'} | label = {label!r}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC
# MAGIC The client's policy in force, resolved once and applied to every engine.
# MAGIC Comparing engines is only a valid inference if the policy is held
# MAGIC identical — a net value computed under a 14% holding rate is a different
# MAGIC number from one computed under 20%.

# COMMAND ----------

spark.sql(st.build_settings_table_ddl(app_catalog, app_schema))
settings = st.resolve(
    spark.sql(st.build_settings_read_query(app_catalog, app_schema)).toPandas().to_dict("records")
)
settings_json = json.dumps(
    {spec.key: settings[spec.key] for spec in st.SPECS}, sort_keys=True
)
print(settings_json)

# COMMAND ----------

spark.sql(persistence.build_sim_run_table_ddl(app_catalog, app_schema))
spark.sql(persistence.build_sim_run_pair_table_ddl(app_catalog, app_schema))

# CREATE TABLE IF NOT EXISTS will not widen a table that is already there, so a results
# table from an earlier deploy needs one ALTER. Tolerated rather than checked first: a
# column that is already present is the expected case, not a failure.
try:
    spark.sql(persistence.build_sim_run_migration(app_catalog, app_schema))
    print("added the detector-count columns")
except Exception as exc:  # noqa: BLE001 - "already exists" is the normal path
    print(f"sim_run migration skipped: {exc}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run
# MAGIC
# MAGIC The generated world is built once and shared, so every engine faces the
# MAGIC same pairs, the same demand and the same suppliers. Re-drawing per engine
# MAGIC would make a difference in net value indistinguishable from a difference
# MAGIC in the dice.

# COMMAND ----------

runs = run.compare(engines, settings=settings, budget=budget)
batch_id = uuid.uuid4().hex[:12].upper()

for result in runs:
    row = result.to_row()
    print(
        f"{row['CONSUMPTION_MODEL']:16s} "
        f"caught {row['CAUGHT']:3d}  missed {row['MISSED']:3d}  "
        f"false {row['FALSE_ALARMS']:3d}  "
        f"net Rs {row['NET_VALUE']:,.0f}  "
        f"detector net Rs {row['DETECTOR_NET_VALUE']:,.0f}"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write
# MAGIC
# MAGIC `RUN_ID` is deterministic from (engine, budget, settings), so re-running
# MAGIC the same configuration **replaces** its row rather than appending a second
# MAGIC one. The world is deterministic too, so a second row would be a duplicate
# MAGIC of the first and would double-count in any average over the table.

# COMMAND ----------

for result in runs:
    row = result.to_row()
    identifier = persistence.run_id(
        engine=result.engine,
        budget=result.budget,
        settings_json=settings_json,
        label=label,
    )

    for statement in persistence.build_delete_run(
        identifier, catalog=app_catalog, schema=app_schema
    ):
        spark.sql(statement)

    spark.sql(
        persistence.build_sim_run_insert(
            row,
            run_identifier=identifier,
            batch_id=batch_id,
            label=label,
            settings_json=settings_json,
            catalog=app_catalog,
            schema=app_schema,
        )
    )
    spark.sql(
        persistence.build_sim_run_pair_insert(
            identifier,
            result.scorecard.pairs,
            catalog=app_catalog,
            schema=app_schema,
        )
    )
    print(f"wrote {identifier} ({result.engine}): {len(result.scorecard.pairs)} pairs")

# COMMAND ----------

dbutils.notebook.exit(batch_id)
