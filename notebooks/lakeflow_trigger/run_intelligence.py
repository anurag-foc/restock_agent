# Databricks notebook source
# MAGIC %md
# MAGIC # Run Intelligence
# MAGIC
# MAGIC Replaces `invoke_supervisor.py`. Detect → suppress → select → narrate → act,
# MAGIC in **one** Supervisor turn.
# MAGIC
# MAGIC The old path needed 2+N turns because Model Serving has a 290s gateway
# MAGIC ceiling and ranking plus per-candidate drill-downs plus the write-up
# MAGIC exceeded it. The redesign removes the reason rather than optimising the
# MAGIC split: the detectors compute every figure and attach it to the finding,
# MAGIC so there is nothing left to drill into. What remains is a write-up and
# MAGIC two tool calls.
# MAGIC
# MAGIC **This notebook never reads the ranked findings to decide anything.** It
# MAGIC counts them and passes a prepared brief. That was structural in the old
# MAGIC design too — an earlier revision handed the Supervisor pre-chewed
# MAGIC candidate JSON and it reasoned straight from that. Here the brief IS the
# MAGIC analysis, computed deterministically upstream and independently testable.

# COMMAND ----------

import sys
from datetime import date, datetime, timezone

import pandas as pd

sys.path.append("../../src")

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from agentic_restock import narration
from agentic_restock import readability
from agentic_restock import settings as st
from agentic_restock.detectors import scanners, selection
from agentic_restock.jobs import positions, run_intelligence, run_log

# COMMAND ----------

dbutils.widgets.text("supervisor_endpoint_name", "", "Supervisor serving endpoint")
dbutils.widgets.text("gold_catalog", "", "Data Engineering catalog override (optional)")
dbutils.widgets.text("dim_schema", "", "Dimension schema override (optional)")
dbutils.widgets.text("facts_schema", "", "Facts schema override (optional)")
dbutils.widgets.text("app_catalog", "", "Application catalog override (optional)")
dbutils.widgets.text("app_schema", "", "Application schema override (optional)")
dbutils.widgets.text("budget", "", "Override items per notification (blank = use settings)")

endpoint_name = dbutils.widgets.get("supervisor_endpoint_name")
if not endpoint_name:
    raise ValueError("supervisor_endpoint_name is required")

gold_catalog = dbutils.widgets.get("gold_catalog") or None
dim_schema = dbutils.widgets.get("dim_schema") or None
facts_schema = dbutils.widgets.get("facts_schema") or None
app_catalog = dbutils.widgets.get("app_catalog") or None
app_schema = dbutils.widgets.get("app_schema") or None
budget_override = dbutils.widgets.get("budget") or None

as_of = date.today()
started_at = datetime.now(timezone.utc)
app_catalog_name = app_catalog or "gold_dev"
app_schema_name = app_schema or "supply_chain_analytics"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC
# MAGIC Client-configurable policy — service level is not here, but what stock
# MAGIC costs to hold, how protective transfers are, and what is worth raising
# MAGIC all are. Created on demand rather than in `schema_bootstrap`, which uses
# MAGIC `CREATE OR REPLACE` and would wipe the client's settings and their change
# MAGIC history on every bootstrap run.
# MAGIC
# MAGIC An empty table resolves to the values that were hardcoded before this
# MAGIC existed, so a missing or unreadable row degrades to *correct* rather than
# MAGIC to zero.

# COMMAND ----------

spark.sql(st.build_settings_table_ddl(app_catalog, app_schema))
settings = st.resolve(
    spark.sql(st.build_settings_read_query(app_catalog, app_schema))
    .toPandas()
    .to_dict("records")
)
budget = int(budget_override) if budget_override else settings.items_per_notification
settings_snapshot = st.snapshot_json(settings)
print(f"settings in force: {settings_snapshot}")


def ensure_run_log():
    """Create the log table, and widen a pre-settings one. Never fatal.

    The ALTER fails once the column exists, which is the normal case on every run after
    the first. A missing audit column must not stop a scan, so the failure is swallowed
    rather than checked for.
    """
    spark.sql(run_log.build_run_log_table_ddl(app_catalog, app_schema))
    try:
        spark.sql(run_log.build_run_log_migration(app_catalog, app_schema))
    except Exception:
        pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## Detect
# MAGIC
# MAGIC Reads the corrected-measures layer that `refresh_positions` built. If
# MAGIC that job has not run, this one has nothing to say — and says so, rather
# MAGIC than reporting a quiet day.

# COMMAND ----------

position_fqn = f"{app_catalog_name}.{app_schema_name}.{positions.PART_POSITION_TABLE}"
cascade_fqn = f"{app_catalog_name}.{app_schema_name}.{positions.PARENT_CASCADE_TABLE}"
supplier_fqn = f"{app_catalog_name}.{app_schema_name}.{positions.SUPPLIER_PERFORMANCE_TABLE}"

part_position = spark.sql(f"SELECT * FROM {position_fqn}").toPandas()
parent_cascades = spark.sql(f"SELECT * FROM {cascade_fqn}").toPandas()
supplier_performance = spark.sql(f"SELECT * FROM {supplier_fqn}").toPandas()

if part_position.empty:
    raise AssertionError(
        f"{position_fqn} is empty. Every scanner would find nothing, which is "
        "indistinguishable from a quiet day — run refresh_positions first."
    )

# Business keys resolve to names for the prose only. A failed read degrades to the IDs that
# were printed before, never to blanks -- an unreadable report beats a report full of holes.
try:
    names = readability.names_from_rows(
        spark.sql(readability.build_name_query(gold_catalog, dim_schema))
        .toPandas()
        .to_dict("records")
    )
    print(f"names: {len(names.parts)} parts, {len(names.warehouses)} warehouses, "
          f"{len(names.suppliers)} suppliers")
except Exception as exc:
    print(f"name lookup failed, falling back to IDs: {exc}")
    names = readability.EMPTY_NAMES

found = scanners.scan_all(part_position, parent_cascades, supplier_performance, settings)
print(f"{len(found)} findings across {part_position.shape[0]} part/warehouse pairs")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Suppress
# MAGIC
# MAGIC Drops findings with a live decision against them. In code, never in a
# MAGIC prompt: a model that filters correctly 97% of the time re-raises a
# MAGIC rejected item about monthly, and a PM who sees something they already
# MAGIC rejected stops trusting the queue.

# COMMAND ----------

commitment_rows = spark.sql(f"""
    SELECT
        COALESCE(frr.SUBJECT_KEY, concat(p.PART_ID, '@', w.WAREHOUSE_ID)) AS SUBJECT_KEY,
        drs.REQUEST_STATUS,
        frr.REQUESTED_DATE_KEY,
        frr.DECISION_DATE_KEY,
        COALESCE(pp.MU_LEAD_DAYS, 30.0) AS MU_LEAD_DAYS,
        -- The PM's own reasoning, and what the finding was worth when they gave it. Read back
        -- into the next run so a decision is a record rather than only a gate.
        frr.NOTE,
        frr.DECISION_REASON,
        frr.EXPOSURE_AT_DECISION
    FROM {gold_catalog or 'gold_dev'}.{facts_schema or 'supply_chain_analytics'}.fact_restock_request frr
    JOIN {gold_catalog or 'gold_dev'}.dim.dim_request_status drs
      ON drs.REQUEST_STATUS_KEY = frr.REQUEST_STATUS_KEY
    LEFT JOIN {gold_catalog or 'gold_dev'}.dim.dim_part p
      ON p.PART_KEY = frr.PART_KEY AND p.IS_CURRENT = true
    LEFT JOIN {gold_catalog or 'gold_dev'}.dim.dim_warehouse w
      ON w.WAREHOUSE_KEY = frr.WAREHOUSE_KEY
    LEFT JOIN {position_fqn} pp
      ON pp.PART_ID = p.PART_ID AND pp.WAREHOUSE_ID = w.WAREHOUSE_ID
    WHERE frr.REQUESTED_DATE_KEY > 0
""").toPandas()


def _as_date(value):
    # NULL arrives from pandas as float NaN, not None, and `NaN is None` is
    # False -- so the None check passes it straight to int() and raises. Every
    # undecided line has a NULL DECISION_DATE_KEY, which is most of them.
    if value is None or pd.isna(value):
        return None
    key = int(value)
    if key <= 0:
        return None
    return date(key // 10000, (key // 100) % 100, key % 100)


commitments = [
    selection.Commitment(
        suppression_key=row.SUBJECT_KEY,
        status=row.REQUEST_STATUS,
        requested_on=_as_date(row.REQUESTED_DATE_KEY),
        decided_on=_as_date(row.DECISION_DATE_KEY),
        lead_days=float(row.MU_LEAD_DAYS or 30.0),
        note=row.NOTE,
        reason=row.DECISION_REASON,
        exposure_at_decision=(
            None if row.EXPOSURE_AT_DECISION is None or pd.isna(row.EXPOSURE_AT_DECISION)
            else float(row.EXPOSURE_AT_DECISION)
        ),
    )
    for row in commitment_rows.itertuples(index=False)
    if _as_date(row.REQUESTED_DATE_KEY) is not None
]

kept = selection.apply_suppression(found, commitments, as_of=as_of)
print(f"{len(found)} -> {len(kept)} after suppression ({len(commitments)} open decisions)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select
# MAGIC
# MAGIC Collapses findings that describe one root cause, then applies the hard
# MAGIC output budget with a diversity constraint. Scarcity is the feature: one
# MAGIC real MRP run produced 8,366 action messages in a week.

# COMMAND ----------

selected = selection.select(kept, budget=budget)
report = selection.selection_report(selection.collapse_duplicates(kept), selected)
print(report)

if not selected:
    # A genuinely quiet run. Logged so "nothing needed attention" and "the job
    # silently broke" stay distinguishable from outside — which is what the
    # alert-fatigue counterpoint rests on.
    ensure_run_log()
    spark.sql(
        run_log.build_run_log_insert(
            candidate_count=len(kept),
            outcome="NO_ACTION",
            note=str(report),
            app_catalog=app_catalog,
            app_schema=app_schema,
            settings_snapshot=settings_snapshot,
        )
    )
    dbutils.notebook.exit("NO_ACTION")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Narrate and act
# MAGIC
# MAGIC One turn. The brief carries the evidence, the output skeleton, and the
# MAGIC arithmetic already performed — so the model writes prose around figures
# MAGIC it cannot recompute, and passes through tool arguments already resolved
# MAGIC to PART_IDs.

# COMMAND ----------

brief = narration.build_brief(selected, report, names)
print(f"brief: {len(brief.text)} chars, {len(brief.quote_lines)} quote lines")
if brief.unpersistable:
    # Should be empty. A finding at a grain the table cannot express would be
    # silently dropped otherwise.
    raise AssertionError(
        "findings with no part and no supplier cannot be persisted: "
        + ", ".join(f.subject_id for f in brief.unpersistable)
    )

# Timeouts MUST go through Config. As kwargs they are rejected; set on w.config
# afterwards they are read too late and silently ignored, and the symptom is a
# TimeoutError at exactly five minutes.
w = WorkspaceClient(
    config=Config(
        http_timeout_seconds=run_intelligence.TURN_TIMEOUT_SECONDS,
        retry_timeout_seconds=run_intelligence.TURN_TIMEOUT_SECONDS,
    )
)

result = run_intelligence.run(
    w,
    endpoint_name,
    brief=brief.text,
    expected_lines=len(brief.quote_lines),
    as_of=as_of,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Check what it wrote
# MAGIC
# MAGIC Every figure in the report should be one the detectors computed. Until
# MAGIC now nothing checked that: the turn verified the tools were *called*, not
# MAGIC that the sentences were true — the same "the rule was in its
# MAGIC instructions" posture that produced all three fabrications on record.
# MAGIC
# MAGIC This runs after `persist_quote`, because the model calls that itself, so
# MAGIC it reports rather than prevents. That is a real limit: it catches an
# MAGIC invented figure on the run that made it, not before a PM can read it.
# MAGIC Preventing it would mean splitting the turn, which is the thing the
# MAGIC redesign removed. Loud and recorded beats silent.

# COMMAND ----------

prose_problems = readability.verify_report(result.text, selected)
for index, verdict in prose_problems:
    where = f"item {index}" if index else "the report"
    print(f"UNVERIFIED FIGURE in {where}: {verdict.reason}")
if not prose_problems:
    print(f"every figure in the report traces to measured evidence ({len(selected)} items)")

# COMMAND ----------

summary = run_intelligence.summarise(result, report)
if prose_problems:
    # On the run log, not only in the job output. A job log is read when someone already
    # suspects something; the run log is what gets read when they are asking what happened.
    summary = summary.rstrip("}") + ', "unverified_figures": ' + str(len(prose_problems)) + "}"
print(summary)

ensure_run_log()
spark.sql(
    run_log.build_run_log_insert(
        candidate_count=len(selected),
        outcome="SUPERVISOR_INVOKED",
        note=summary,
        app_catalog=app_catalog,
        app_schema=app_schema,
        settings_snapshot=settings_snapshot,
    )
)

elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
print(f"completed in {elapsed:.0f}s across {result.approval_rounds} approval round(s)")
dbutils.notebook.exit(summary)
