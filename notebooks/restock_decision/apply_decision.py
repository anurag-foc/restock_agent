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

dbutils.widgets.text("gold_catalog", "", "Data Engineering catalog override (optional)")
# Defaults to gold_dev so the existing pipeline is unaffected. The redesign runs on the
# gold_dev_analytics replica, and a decision written to the wrong catalog does not fail loudly --
# the UPDATE simply matches no rows and the PM's approval vanishes.
GOLD = dbutils.widgets.get("gold_catalog") or "gold_dev"
print(f"catalog: {GOLD}")


VALID_REASONS = {
    "NOT_A_PROBLEM",
    "ALREADY_HANDLED",
    "CANNOT_ACT_NOW",
    "NUMBERS_WRONG",
    "OTHER",
}

decisions = json.loads(dbutils.widgets.get("decisions_json") or "[]")
if not decisions:
    raise ValueError("decisions_json parameter is required and must be a non-empty JSON array")

for d in decisions:
    d["decision"] = str(d.get("decision", "")).strip().upper()
    if d["decision"] not in ("APPROVED", "REJECTED"):
        raise ValueError(f"decision must be APPROVED or REJECTED, got: {d.get('decision')!r}")
    d["restock_request_key"] = int(d["restock_request_key"])
    d["note"] = (d.get("note") or "").strip()
    # The structured reason the PM picked. It is what changes the system's behaviour next run:
    # CANNOT_ACT_NOW is a snooze that returns after two weeks, NOT_A_PROBLEM and NUMBERS_WRONG
    # close the finding until its value materially grows. Free text cannot carry that
    # distinction reliably -- "no" and "not now" look the same and mean opposite things.
    d["reason"] = (d.get("reason") or "").strip().upper() or None
    if d["reason"] and d["reason"] not in VALID_REASONS:
        raise ValueError(f"reason must be one of {sorted(VALID_REASONS)}, got: {d['reason']!r}")

# COMMAND ----------

results = []
approved_keys = []
advisories = []

for d in decisions:
    line_key = d["restock_request_key"]
    decision = d["decision"]
    note = d["note"]

    current = spark.sql(f"""
        SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL, dp.PART_ID, dw.WAREHOUSE_ID
        FROM {GOLD}.supply_chain_analytics.fact_restock_request frr
        JOIN {GOLD}.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
        -- LEFT: a supplier-grain line has no PART_KEY (schema_changes §1.5), and an inner join
        -- would report it NOT_FOUND, which now fails the whole job.
        LEFT JOIN {GOLD}.dim.dim_part dp ON frr.PART_KEY = dp.PART_KEY AND dp.IS_CURRENT = true
        LEFT JOIN {GOLD}.dim.dim_warehouse dw ON frr.WAREHOUSE_KEY = dw.WAREHOUSE_KEY
        WHERE frr.RESTOCK_REQUEST_KEY = {line_key}
    """).collect()

    if not current:
        results.append({"restock_request_key": line_key, "outcome": "NOT_FOUND"})
        print(f"Line {line_key}: no fact_restock_request row -- skipped.")
        continue

    current_status = current[0]["REQUEST_STATUS"]
    urgency = current[0]["URGENCY_LEVEL"]
    part_id = current[0]["PART_ID"]
    warehouse_id = current[0]["WAREHOUSE_ID"]

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

    # An approval goes STRAIGHT to FULFILLING -- there is no separate APPROVED state waiting on
    # a machine step any more. The old flow inserted a Supervisor turn between the two, which
    # asked a Genie space to re-check live stock before acting. That check was purchase-shaped:
    # meaningful for PURCHASE and TRANSFER, meaningless for RECALIBRATE (change a planning
    # parameter), REVIEW_STOCK (go look at idle stock), and the three supplier-grain finding
    # types. Approving now means "yes, do this", and the PM marks it done from the app when
    # they have.
    #
    # CONFIRMED_QTY is set here for the same reason: it used to be the fulfillment tool's job,
    # and approving at the proposed quantity is what an approval means.
    target_status = "FULFILLING" if decision == "APPROVED" else decision
    note_literal = "NULL" if not note else "'" + note.replace("'", "''") + "'"
    reason = d["reason"]
    reason_literal = "NULL" if not reason else "'" + reason + "'"

    # dim_request_status does not cover every (status x urgency) combination -- in gold_dev
    # today, REJECTED exists only for LOW and FULFILLING only for CRITICAL/HIGH. The UPDATE
    # below resolves the new key with a subquery, so a missing combination yields NULL and the
    # write dies as [DELTA_NOT_NULL_CONSTRAINT_VIOLATED] on REQUEST_STATUS_KEY, which names a
    # column rather than the actual problem. Checked up front so the message says what to fix.
    target_key = spark.sql(f"""
        SELECT MIN(REQUEST_STATUS_KEY) AS k
        FROM {GOLD}.dim.dim_request_status
        WHERE REQUEST_STATUS = '{target_status}' AND URGENCY_LEVEL = '{urgency}'
    """).collect()[0]["k"]
    if target_key is None:
        raise AssertionError(
            f"{GOLD}.dim.dim_request_status has no row for "
            f"REQUEST_STATUS='{target_status}' AND URGENCY_LEVEL='{urgency}', so line {line_key} "
            f"cannot be moved there. This is a gap in the dimension, not in the decision: the "
            f"(status x urgency) matrix is incomplete and needs the missing combinations added."
        )
    confirmed_clause = (
        "CONFIRMED_QTY = REQUESTED_QTY, VARIANCE_QTY = 0," if decision == "APPROVED" else ""
    )
    spark.sql(f"""
        UPDATE {GOLD}.supply_chain_analytics.fact_restock_request
        SET
            REQUEST_STATUS_KEY = {target_key},
            {confirmed_clause}
            DECISION_DATE_KEY = CAST(date_format(current_date(), 'yyyyMMdd') AS INT),
            NOTE = {note_literal},
            DECISION_REASON = {reason_literal}
        WHERE RESTOCK_REQUEST_KEY = {line_key}
    """)

    results.append({"restock_request_key": line_key, "outcome": decision, "urgency": urgency})
    print(f"Line {line_key} ({urgency}): {current_status} -> {target_status}")
    if decision == "APPROVED":
        approved_keys.append(line_key)

        # The one thing the retired guardrail genuinely caught: a purchase approved days after
        # it was raised, when an open PO already covers it. Kept, but as a deterministic check
        # here rather than an LLM round-trip -- and advisory, not blocking, because the PM has
        # just said to do it and a machine should not quietly overrule that. Only lines with a
        # part can be checked; a supplier-grain action has no stock position.
        if part_id and warehouse_id:
            covered = spark.sql(f"""
                SELECT {GOLD}.supply_chain_analytics.pending_procurement_qty(
                    '{part_id}', '{warehouse_id}') AS qty
            """).collect()[0]["qty"]
            if covered and covered > 0:
                advisories.append(
                    f"Line {line_key} ({part_id} @ {warehouse_id}): {covered} units already on "
                    f"open purchase orders at approval time."
                )
                print(f"  ADVISORY: {covered} units already on open POs for {part_id}")

# COMMAND ----------

# Every line missing is a wiring problem, not a business state. A single NOT_FOUND can be a
# stale submit; ALL of them means the job is reading a different catalog from the one the app
# wrote to, and the PM's approvals have silently gone nowhere. Without this the job reports
# SUCCESS having applied nothing.
if results and all(r["outcome"] == "NOT_FOUND" for r in results):
    raise AssertionError(
        f"None of the {len(results)} submitted line(s) exist in "
        f"{GOLD}.supply_chain_analytics.fact_restock_request. Check the gold_catalog parameter "
        f"matches the catalog the review app reads."
    )

for a in advisories:
    print("ADVISORY: " + a)

dbutils.jobs.taskValues.set(key="results_json", value=json.dumps(results))
dbutils.jobs.taskValues.set(key="approved_keys_json", value=json.dumps(approved_keys))
dbutils.jobs.taskValues.set(key="approved_count", value=str(len(approved_keys)))
dbutils.jobs.taskValues.set(key="advisories_json", value=json.dumps(advisories))

print(
    f"{len(results)} line(s): "
    f"{len(approved_keys)} approved -> FULFILLING, "
    f"{sum(1 for r in results if r['outcome'] == 'REJECTED')} rejected, "
    f"{len(advisories)} advisory note(s)."
)
