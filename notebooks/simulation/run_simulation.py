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
from agentic_restock.simulation import baseline, benchmark, persistence, reasoning, run

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
spark.sql(persistence.build_sim_benchmark_table_ddl(app_catalog, app_schema))
spark.sql(persistence.build_sim_selection_table_ddl(app_catalog, app_schema))
spark.sql(persistence.build_sim_type_summary_table_ddl(app_catalog, app_schema))

# CREATE TABLE IF NOT EXISTS will not widen a table that is already there, so a results
# table from an earlier deploy needs one ALTER. Tolerated rather than checked first: a
# column that is already present is the expected case, not a failure.
try:
    spark.sql(persistence.build_sim_run_migration(app_catalog, app_schema))
    print("added the detector-count columns")
except Exception as exc:  # noqa: BLE001 - "already exists" is the normal path
    print(f"sim_run migration skipped: {exc}")

try:
    spark.sql(persistence.build_sim_type_summary_migration(app_catalog, app_schema))
    print("added the accuracy-note column")
except Exception as exc:  # noqa: BLE001 - "already exists" is the normal path
    print(f"sim_type_summary migration skipped: {exc}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run
# MAGIC
# MAGIC The generated world is built once and shared, so every engine faces the
# MAGIC same pairs, the same demand and the same suppliers. Re-drawing per engine
# MAGIC would make a difference in net value indistinguishable from a difference
# MAGIC in the dice.

# COMMAND ----------

# The world once, shared by every arm. Two arms compared on two independently drawn worlds
# is a comparison of the dice.
frames = run.world()

# The incumbent ERP rules go first, so the comparison has an origin. Without them the page
# compares two of our own estimators against each other, which tells a client nothing --
# they have no reference point for either. See simulation/baseline.py.
incumbents = baseline.run_all(settings=settings, budget=budget, world_frames=frames)
ours = [
    run.run_engine(engine, settings=settings, budget=budget, world_frames=frames)
    for engine in engines
]
runs = incumbents + ours
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
    # What this arm actually put in front of a person. The counts above say how many were
    # right; this says what they were, which is the part that shows four different kinds of
    # problem where a reorder rule produces twenty of the same sentence.
    shown = result.selected if result.budget > 0 else result.detected
    selection_sql = persistence.build_sim_selection_insert(
        shown,
        run_identifier=identifier,
        batch_id=batch_id,
        engine=result.engine,
        catalog=app_catalog,
        schema=app_schema,
    )
    if selection_sql:
        spark.sql(selection_sql)

    # Counts + one worked example per finding type -- the benchmark chart's data. Built from
    # every finding this arm produced (budget ignored), so the chart shows the full breadth of
    # what the detectors are capable of, not just what one run's output cap let through.
    #
    # `truth=result.truth` adds the plain-language accuracy note for the three shortage-
    # addressing types (STOCKOUT_RISK, CASCADE_BLOCK, REDEPLOYMENT) -- every arm here, ERP
    # included, carries its own truth from `EngineRun.truth`, so the incumbent's own hit rate
    # is shown on the same honest footing as ours rather than only checking our side.
    summary_rows = reasoning.type_summary_rows(result.detected, truth=result.truth)
    summary_sql = persistence.build_sim_type_summary_insert(
        summary_rows,
        run_identifier=identifier,
        batch_id=batch_id,
        label=label,
        engine=result.engine,
        catalog=app_catalog,
        schema=app_schema,
    )
    if summary_sql:
        spark.sql(summary_sql)

    print(
        f"wrote {identifier} ({result.engine}): "
        f"{len(result.scorecard.pairs)} pairs, {len(shown)} surfaced, "
        f"{len(summary_rows)} finding types"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The benchmark
# MAGIC
# MAGIC `sim_run` scores one question — will these pairs run short — which is
# MAGIC the right question for three of the eight scanners and says nothing
# MAGIC about the other five. The benchmark grades the eleven problems planted
# MAGIC in `generation/scenarios.py` before any of this existed, two of which
# MAGIC require the system to stay **quiet**.
# MAGIC
# MAGIC Graded on the first engine arm, which is the configuration a PM is
# MAGIC actually running. The incumbent rules are not graded against it: a
# MAGIC reorder-point rule has one kind of answer and would fail nine of the
# MAGIC eleven by construction, which measures nothing.

# COMMAND ----------

graded = ours[0]
card = benchmark.run(graded.detected, graded.selected, pair_count=len(frames["position"]))

for check in card.checks:
    print(f"{check.result:<5} {check.finding_id:<4} {check.name} -- {check.detail}")
print(
    f"\n{card.passed} passed, {card.failed} failed, {card.needs_check} to look at "
    f"(of {card.total}) | kinds proven: {len(card.types_covered)} of {card.types_total}"
)

spark.sql(
    persistence.build_sim_benchmark_insert(
        card.checks,
        batch_id=batch_id,
        label=label,
        catalog=app_catalog,
        schema=app_schema,
    )
)
print(f"wrote {len(card.checks)} benchmark rows for batch {batch_id}")

# COMMAND ----------

dbutils.notebook.exit(batch_id)
