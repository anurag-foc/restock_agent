# Databricks notebook source
# MAGIC %md
# MAGIC # Task 1 — Apply the PM's approve/reject decisions
# MAGIC
# MAGIC Deterministic status write, no LLM involved. Triggered by the
# MAGIC restock-review app's Final Submit button, which batches every part-line
# MAGIC the PM staged a decision for on one quote into a single job run.
# MAGIC
# MAGIC `REQUEST_STATUS_KEY` is a FK into `dim_request_status`, which enumerates
# MAGIC (REQUEST_STATUS x URGENCY_LEVEL x DECISION) combinations — so the new key
# MAGIC has to be resolved against *each line's* current urgency, otherwise every
# MAGIC non-CRITICAL line silently gets relabelled CRITICAL.
# MAGIC
# MAGIC Sets `approved_keys_json` / `approved_count` as task values; the
# MAGIC `has_approval` condition task branches on `approved_count > 0` so the
# MAGIC fulfillment task only runs (once, for every approved line) when at least
# MAGIC one line in the batch was approved.

# COMMAND ----------

import json

dbutils.widgets.text(
    "decisions_json", "[]",
    "JSON array of {restock_request_key, decision, note}",
)

decisions = json.loads(dbutils.widgets.get("decisions_json") or "[]")
if not decisions:
    raise ValueError("decisions_json parameter is required and must be a non-empty JSON array")

for d in decisions:
    d["decision"] = str(d.get("decision", "")).strip().upper()
    if d["decision"] not in ("APPROVED", "REJECTED"):
        raise ValueError(f"decision must be APPROVED or REJECTED, got: {d.get('decision')!r}")
    d["restock_request_key"] = int(d["restock_request_key"])
    d["note"] = (d.get("note") or "").strip()

# COMMAND ----------

results = []
approved_keys = []

for d in decisions:
    line_key = d["restock_request_key"]
    decision = d["decision"]
    note = d["note"]

    current = spark.sql(f"""
        SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL
        FROM gold_dev.supply_chain_analytics.fact_restock_request frr
        JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
        WHERE frr.RESTOCK_REQUEST_KEY = {line_key}
    """).collect()

    if not current:
        results.append({"restock_request_key": line_key, "outcome": "NOT_FOUND"})
        print(f"Line {line_key}: no fact_restock_request row -- skipped.")
        continue

    current_status = current[0]["REQUEST_STATUS"]
    urgency = current[0]["URGENCY_LEVEL"]

    # A line can be re-decided from PENDING_APPROVAL (first decision) or from
    # NEEDS_REVIEW (the fulfillment guardrail flagged it after approval --
    # e.g. an open PO now covers the request -- and the PM is re-deciding
    # whether to retry or cancel it). Anything else (already APPROVED,
    # REJECTED, FULFILLING, COMPLETED) is a stale/duplicate submit and is a
    # no-op for idempotency.
    if current_status not in ("PENDING_APPROVAL", "NEEDS_REVIEW"):
        results.append({"restock_request_key": line_key, "outcome": "NOOP", "current_status": current_status})
        print(f"Line {line_key} is already {current_status} -- no change applied.")
        continue

    note_literal = "NULL" if not note else "'" + note.replace("'", "''") + "'"
    spark.sql(f"""
        UPDATE gold_dev.supply_chain_analytics.fact_restock_request
        SET
            REQUEST_STATUS_KEY = (
                SELECT MIN(REQUEST_STATUS_KEY)
                FROM gold_dev.dim.dim_request_status
                WHERE REQUEST_STATUS = '{decision}' AND URGENCY_LEVEL = '{urgency}'
            ),
            DECISION_DATE_KEY = CAST(date_format(current_date(), 'yyyyMMdd') AS INT),
            NOTE = {note_literal}
        WHERE RESTOCK_REQUEST_KEY = {line_key}
    """)

    results.append({"restock_request_key": line_key, "outcome": decision, "urgency": urgency})
    print(f"Line {line_key} ({urgency}): PENDING_APPROVAL -> {decision}")
    if decision == "APPROVED":
        approved_keys.append(line_key)

# COMMAND ----------

dbutils.jobs.taskValues.set(key="results_json", value=json.dumps(results))
dbutils.jobs.taskValues.set(key="approved_keys_json", value=json.dumps(approved_keys))
dbutils.jobs.taskValues.set(key="approved_count", value=str(len(approved_keys)))
