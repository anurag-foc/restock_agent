# Databricks notebook source
# MAGIC %md
# MAGIC # Task 1 — Apply the PM's approve/reject decision
# MAGIC
# MAGIC Deterministic status write, no LLM involved. Triggered by the
# MAGIC restock-review app the moment a Production Manager decides a single
# MAGIC `fact_restock_request` line.
# MAGIC
# MAGIC `REQUEST_STATUS_KEY` is a FK into `dim_request_status`, which enumerates
# MAGIC (REQUEST_STATUS x URGENCY_LEVEL x DECISION) combinations — so the new key
# MAGIC has to be resolved against *this line's* current urgency, otherwise every
# MAGIC non-CRITICAL line silently gets relabelled CRITICAL.
# MAGIC
# MAGIC Sets `decision_applied` as a task value; the `has_approval` condition task
# MAGIC branches on it so the fulfillment task only runs for APPROVED.

# COMMAND ----------

dbutils.widgets.text("restock_request_key", "", "fact_restock_request.RESTOCK_REQUEST_KEY")
dbutils.widgets.text("decision", "", "APPROVED or REJECTED")

restock_request_key = dbutils.widgets.get("restock_request_key")
decision = dbutils.widgets.get("decision").strip().upper()

if not restock_request_key:
    raise ValueError("restock_request_key parameter is required")
if decision not in ("APPROVED", "REJECTED"):
    raise ValueError(f"decision must be APPROVED or REJECTED, got: {decision!r}")

line_key = int(restock_request_key)

# COMMAND ----------

current = spark.sql(f"""
    SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL
    FROM gold_dev.supply_chain_analytics.fact_restock_request frr
    JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
    WHERE frr.RESTOCK_REQUEST_KEY = {line_key}
""").collect()

if not current:
    raise ValueError(f"No fact_restock_request row with RESTOCK_REQUEST_KEY={line_key}")

current_status = current[0]["REQUEST_STATUS"]
urgency = current[0]["URGENCY_LEVEL"]

# Idempotency: re-running this task (or a double-click upstream) must not
# overwrite a decision that has already moved on.
if current_status != "PENDING_APPROVAL":
    print(f"Line {line_key} is already {current_status} -- no change applied.")
    dbutils.jobs.taskValues.set(key="decision_applied", value="NOOP")
    dbutils.jobs.taskValues.set(key="restock_request_key", value=str(line_key))
    dbutils.notebook.exit(f"NOOP: already {current_status}")

# COMMAND ----------

spark.sql(f"""
    UPDATE gold_dev.supply_chain_analytics.fact_restock_request
    SET
        REQUEST_STATUS_KEY = (
            SELECT MIN(REQUEST_STATUS_KEY)
            FROM gold_dev.dim.dim_request_status
            WHERE REQUEST_STATUS = '{decision}' AND URGENCY_LEVEL = '{urgency}'
        ),
        DECISION_DATE_KEY = CAST(date_format(current_date(), 'yyyyMMdd') AS INT)
    WHERE RESTOCK_REQUEST_KEY = {line_key}
""")

print(f"Line {line_key} ({urgency}): PENDING_APPROVAL -> {decision}")

dbutils.jobs.taskValues.set(key="decision_applied", value=decision)
dbutils.jobs.taskValues.set(key="restock_request_key", value=str(line_key))
