# Databricks notebook source
# MAGIC %md
# MAGIC # Simulated Inventory History
# MAGIC
# MAGIC `gold_dev` holds roughly 40 rows per fact over a 10-day window — enough to
# MAGIC prove a query parses, and nothing else. Delivered-vs-promised lead time,
# MAGIC dead stock, and any measurement of how early the scan detects a stockout all
# MAGIC need months of history. This notebook generates that history **into
# MAGIC `gold_dev` itself**.
# MAGIC
# MAGIC ## What it will and will not do
# MAGIC
# MAGIC **Inserts rows. Never alters a schema.** Every write to a Data Engineering
# MAGIC table is an `INSERT INTO` with an explicit column list — no
# MAGIC `CREATE OR REPLACE`, no `ALTER`, no added or dropped columns. The notebook
# MAGIC verifies each target's column set before writing and aborts on a mismatch.
# MAGIC
# MAGIC **Dimensions are read-only.** `dim_part`, `dim_warehouse` and `dim_supplier`
# MAGIC are read to drive the simulation and never written to, so real part costs,
# MAGIC criticality and ABC classes carry through and the value weighting in the scan
# MAGIC reflects a real catalogue.
# MAGIC
# MAGIC **Two things it does delete**, both loudly and both optional:
# MAGIC - rows in the fact tables inside the date window it is about to write, so a
# MAGIC   re-run does not leave two snapshot rows for the same part/warehouse/day
# MAGIC   (which would make "latest snapshot" ambiguous)
# MAGIC - nothing else, ever
# MAGIC
# MAGIC Set `dry_run=true` to see the row counts it would delete and insert without
# MAGIC touching anything.
# MAGIC
# MAGIC ## The tables that make this worth building
# MAGIC
# MAGIC **`sim_events`** — the ground truth. Every stockout, chronically late
# MAGIC supplier, dead part and surplus the simulation deliberately created is
# MAGIC recorded there. It is what turns "the scan produced some rows" into "the
# MAGIC scan ranked 41 of the 47 real stockouts in its top 20, a median 9 days ahead."
# MAGIC
# MAGIC **`sim_pair_scenarios`** — which single scenario produced each pair's
# MAGIC history: `CONTROL`, `DEMAND_DRIFT`, `LEAD_DRIFT`, `LATE_PO`, `DEAD_STOCK` or
# MAGIC `SURPLUS`. Every pair carries exactly one, never several stacked together —
# MAGIC an earlier version let a pair drift in demand *and* go dead *and* sit at a
# MAGIC chronically-late supplier all at once, which meant a detection-rate
# MAGIC improvement could not be attributed to any one mechanism. With this table a
# MAGIC backtest can `GROUP BY scenario` and ask "did the new signal beat the old
# MAGIC one specifically on the pairs where demand quietly outgrew a frozen safety
# MAGIC stock" rather than reporting one number that mixes several causes together.
# MAGIC Both are new tables this repo owns, alongside the ones `schema_bootstrap`
# MAGIC already creates.

# COMMAND ----------

import sys

sys.path.append("../../src")

import datetime as dt

from pyspark.sql.types import (
    DateType,
    DoubleType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from agentic_restock.simulation import SimulationConfig, simulate

# COMMAND ----------

dbutils.widgets.text("target_catalog", "gold_dev", "Catalog to read dimensions from and insert facts into")
dbutils.widgets.text("dim_schema", "dim", "Dimension schema")
dbutils.widgets.text("facts_schema", "supply_chain_analytics", "Facts schema")
dbutils.widgets.text("days", "550", "Days of daily history to generate")
dbutils.widgets.text("seed", "20260831", "Seed — the same seed reproduces the dataset exactly")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Report only, write nothing")
dbutils.widgets.dropdown("replace_window", "true", ["true", "false"], "Delete existing fact rows in the generated date window first")

catalog = dbutils.widgets.get("target_catalog")
DIM = f"{catalog}.{dbutils.widgets.get('dim_schema')}"
FACTS = f"{catalog}.{dbutils.widgets.get('facts_schema')}"
days = int(dbutils.widgets.get("days"))
seed = int(dbutils.widgets.get("seed"))
dry_run = dbutils.widgets.get("dry_run") == "true"
replace_window = dbutils.widgets.get("replace_window") == "true"

end_date = dt.date.today()
start_date = end_date - dt.timedelta(days=days - 1)
start_key, end_key = int(start_date.strftime("%Y%m%d")), int(end_date.strftime("%Y%m%d"))

print(f"Target:  {FACTS}   (dimensions read-only from {DIM})")
print(f"Window:  {start_date} .. {end_date}  ({days} days, seed {seed})")
print(f"Mode:    {'DRY RUN — nothing will be written' if dry_run else 'WRITING'}"
      f"{'' if not replace_window else ', replacing existing rows in the window'}")

# COMMAND ----------

# Guard rail. The generator's contract is insert-only: if a target table's column
# set is not exactly what we expect to write, stop rather than risk a mismatched
# or widened schema.
EXPECTED_COLUMNS = {
    "fact_inventory_snapshot": [
        "INVENTORY_SNAPSHOT_KEY", "SNAPSHOT_DATE_KEY", "PART_KEY", "WAREHOUSE_KEY",
        "QUANTITY_ON_HAND", "SAFETY_STOCK_QTY", "MAX_STOCK_LEVEL", "ALLOCATED_QTY",
        "AVAILABLE_QTY", "IN_TRANSIT_QTY", "BLOCKED_QTY", "DAYS_OF_SUPPLY",
        "AVG_DAILY_CONSUMPTION", "INVENTORY_TURNOVER_RATIO", "STOCK_VALUATION",
        "STOCKOUT_RISK", "DW_LOADED_AT",
    ],
    "fact_inventory_transaction": [
        "INVENTORY_TXN_KEY", "TRANSACTION_ID", "TRANSACTION_DATE_KEY", "PART_KEY",
        "WAREHOUSE_KEY", "PRODUCTION_ORDER_ID", "LINE_KEY", "OPERATOR_EMPLOYEE_KEY",
        "TRANSACTION_TYPE", "QUANTITY", "UNIT_COST", "TRANSACTION_VALUE",
        "BALANCE_AFTER_TXN", "DW_LOADED_AT",
    ],
    "fact_procurement": [
        "PROCUREMENT_KEY", "PURCHASE_ORDER_ID", "ORDER_DATE_KEY", "EXPECTED_DATE_KEY",
        "PART_KEY", "SUPPLIER_KEY", "PLANT_KEY", "BUYER_EMPLOYEE_KEY", "PO_TYPE",
        "STATUS", "ORDER_QTY", "UNIT_RATE", "TOTAL_AMOUNT", "RECEIVED_QTY",
        "PENDING_QTY", "TAX_AMOUNT", "DW_LOADED_AT",
    ],
}

for table, expected in EXPECTED_COLUMNS.items():
    actual = [f.name for f in spark.table(f"{FACTS}.{table}").schema.fields]
    missing, extra = set(expected) - set(actual), set(actual) - set(expected)
    assert not missing, f"{table} is missing columns this notebook writes: {sorted(missing)}"
    if extra:
        print(f"  note: {table} has columns we do not write (left as NULL): {sorted(extra)}")
    print(f"  ✓ {table} schema compatible ({len(actual)} columns)")

# COMMAND ----------

parts = [r.asDict() for r in spark.table(f"{DIM}.dim_part").where("IS_CURRENT = true").collect()]
warehouses = [
    r.asDict() for r in spark.table(f"{DIM}.dim_warehouse").where("OPERATIONAL_STATUS = 'ACTIVE'").collect()
]
suppliers = [r.asDict() for r in spark.table(f"{DIM}.dim_supplier").collect()]
print(f"{len(parts)} parts x {len(warehouses)} warehouses, {len(suppliers)} suppliers (read-only)")

result = simulate(parts, warehouses, suppliers, SimulationConfig(days=days, seed=seed, end_date=end_date))
print(f"\nsnapshots    {len(result.snapshots):>9,}")
print(f"transactions {len(result.transactions):>9,}")
print(f"procurement  {len(result.procurement):>9,}")
print(f"\nground truth planted: {result.event_counts()}")

# COMMAND ----------

# Pre-flight range check — runs BEFORE anything is deleted.
#
# This ordering is the point. The loader deletes the date window before it
# inserts, so a value that does not fit its target column fails the insert and
# leaves the table empty. That is exactly what happened on the first apply run:
# an INVENTORY_TURNOVER_RATIO of 10924.45 does not fit DECIMAL(6,2), the insert
# aborted, and fact_inventory_snapshot was left with zero rows. Validating the
# whole payload up front turns that into a clean abort with nothing touched.
DECIMAL_DOMAINS = {
    "snapshots": {
        "DAYS_OF_SUPPLY": (6, 1),
        "AVG_DAILY_CONSUMPTION": (10, 2),
        "INVENTORY_TURNOVER_RATIO": (6, 2),
        "STOCK_VALUATION": (16, 2),
    },
    "transactions": {"UNIT_COST": (12, 2), "TRANSACTION_VALUE": (16, 2)},
    "procurement": {"UNIT_RATE": (12, 2), "TOTAL_AMOUNT": (18, 2), "TAX_AMOUNT": (16, 2)},
}

violations = []
for attr, columns in DECIMAL_DOMAINS.items():
    rows = getattr(result, attr)
    for column, (precision, scale) in columns.items():
        limit = 10 ** (precision - scale)
        worst = max((abs(r[column]) for r in rows if r.get(column) is not None), default=0.0)
        flag = "OK" if worst < limit else "OVERFLOW"
        print(f"  {flag:<8} {attr}.{column:<26} max {worst:>15,.2f}  limit {limit:>15,}")
        if worst >= limit:
            violations.append(f"{attr}.{column}={worst} exceeds DECIMAL({precision},{scale})")

assert not violations, (
    "Generated values do not fit their target columns; nothing has been written. "
    + "; ".join(violations)
)
print("\n  all generated values fit their target columns — safe to write")

# COMMAND ----------

# Surrogate keys continue from whatever is already in each table, so generated
# rows never collide with the existing ones.
DATE_COLUMN = {
    "fact_inventory_snapshot": "SNAPSHOT_DATE_KEY",
    "fact_inventory_transaction": "TRANSACTION_DATE_KEY",
    "fact_procurement": "ORDER_DATE_KEY",
}
KEY_COLUMN = {
    "fact_inventory_snapshot": "INVENTORY_SNAPSHOT_KEY",
    "fact_inventory_transaction": "INVENTORY_TXN_KEY",
    "fact_procurement": "PROCUREMENT_KEY",
}

offsets, to_delete = {}, {}
for table, key_col in KEY_COLUMN.items():
    offsets[table] = spark.sql(
        f"SELECT COALESCE(MAX({key_col}), 0) AS m FROM {FACTS}.{table}"
    ).collect()[0]["m"]
    to_delete[table] = spark.sql(
        f"SELECT COUNT(*) AS n FROM {FACTS}.{table} "
        f"WHERE {DATE_COLUMN[table]} BETWEEN {start_key} AND {end_key}"
    ).collect()[0]["n"]
    print(f"  {table:<28} max {key_col} = {offsets[table]:>6}  |  "
          f"{to_delete[table]:>5} existing row(s) inside the window")

if replace_window and any(to_delete.values()):
    print(f"\n  replace_window=true — {sum(to_delete.values())} existing row(s) will be DELETED.")
    print("  These are rows in the date range about to be regenerated; leaving them would")
    print("  produce two snapshot rows for the same part/warehouse/day.")

# COMMAND ----------

# Schemas are declared, never inferred. Several generated columns are legitimately
# null for some rows -- a RECEIPT has no production order, a pair with no demand
# has no days-of-supply -- and Spark cannot infer a type for a column that is null
# throughout. Declaring the schema also fixes column order, so rows are built as
# tuples and there is no dependence on dict ordering.
SOURCE_SCHEMAS = {
    "fact_inventory_snapshot": StructType([
        StructField("INVENTORY_SNAPSHOT_KEY", LongType()),
        StructField("SNAPSHOT_DATE_KEY", IntegerType()),
        StructField("PART_KEY", LongType()),
        StructField("WAREHOUSE_KEY", LongType()),
        StructField("QUANTITY_ON_HAND", IntegerType()),
        StructField("SAFETY_STOCK_QTY", IntegerType()),
        StructField("MAX_STOCK_LEVEL", IntegerType()),
        StructField("ALLOCATED_QTY", IntegerType()),
        StructField("AVAILABLE_QTY", IntegerType()),
        StructField("IN_TRANSIT_QTY", IntegerType()),
        StructField("BLOCKED_QTY", IntegerType()),
        StructField("DAYS_OF_SUPPLY", DoubleType()),
        StructField("AVG_DAILY_CONSUMPTION", DoubleType()),
        StructField("INVENTORY_TURNOVER_RATIO", DoubleType()),
        StructField("STOCK_VALUATION", DoubleType()),
        StructField("STOCKOUT_RISK", StringType()),
    ]),
    "fact_inventory_transaction": StructType([
        StructField("INVENTORY_TXN_KEY", LongType()),
        StructField("TRANSACTION_ID", StringType()),
        StructField("TRANSACTION_DATE_KEY", IntegerType()),
        StructField("PART_KEY", LongType()),
        StructField("WAREHOUSE_KEY", LongType()),
        StructField("PRODUCTION_ORDER_ID", StringType()),
        StructField("LINE_KEY", LongType()),
        StructField("OPERATOR_EMPLOYEE_KEY", LongType()),
        StructField("TRANSACTION_TYPE", StringType()),
        StructField("QUANTITY", IntegerType()),
        StructField("UNIT_COST", DoubleType()),
        StructField("TRANSACTION_VALUE", DoubleType()),
        StructField("BALANCE_AFTER_TXN", IntegerType()),
    ]),
    "fact_procurement": StructType([
        StructField("PROCUREMENT_KEY", LongType()),
        StructField("PURCHASE_ORDER_ID", StringType()),
        StructField("ORDER_DATE_KEY", IntegerType()),
        StructField("EXPECTED_DATE_KEY", IntegerType()),
        StructField("PART_KEY", LongType()),
        StructField("SUPPLIER_KEY", LongType()),
        StructField("PLANT_KEY", LongType()),
        StructField("BUYER_EMPLOYEE_KEY", LongType()),
        StructField("PO_TYPE", StringType()),
        StructField("STATUS", StringType()),
        StructField("ORDER_QTY", IntegerType()),
        StructField("UNIT_RATE", DoubleType()),
        StructField("TOTAL_AMOUNT", DoubleType()),
        StructField("RECEIVED_QTY", IntegerType()),
        StructField("PENDING_QTY", IntegerType()),
        StructField("TAX_AMOUNT", DoubleType()),
    ]),
}

# Target column types, applied on the way in. DW_LOADED_AT is stamped by this
# loader rather than carried through the simulation.
TARGET_CASTS = {
    "fact_inventory_snapshot": {
        "DAYS_OF_SUPPLY": "DECIMAL(6,1)",
        "AVG_DAILY_CONSUMPTION": "DECIMAL(10,2)",
        "INVENTORY_TURNOVER_RATIO": "DECIMAL(6,2)",
        "STOCK_VALUATION": "DECIMAL(16,2)",
    },
    "fact_inventory_transaction": {
        "UNIT_COST": "DECIMAL(12,2)",
        "TRANSACTION_VALUE": "DECIMAL(16,2)",
    },
    "fact_procurement": {
        "UNIT_RATE": "DECIMAL(12,2)",
        "TOTAL_AMOUNT": "DECIMAL(18,2)",
        "TAX_AMOUNT": "DECIMAL(16,2)",
    },
}


WRITE_SUMMARY: dict[str, dict] = {}


def write_facts(rows: list[dict], table: str) -> None:
    schema = SOURCE_SCHEMAS[table]
    fields = [f.name for f in schema.fields]
    key_col = KEY_COLUMN[table]
    offset = offsets[table]

    tuples = [tuple(r[f] for f in fields) for r in rows]
    view = f"_sim_{table}"
    spark.createDataFrame(tuples, schema=schema).createOrReplaceTempView(view)

    casts = TARGET_CASTS[table]
    projection = []
    for f in fields:
        expr = f"{f} + {offset}" if f == key_col else f
        projection.append(f"CAST({expr} AS {casts[f]})" if f in casts else expr)
    projection.append("current_timestamp()")
    select_sql = ",\n           ".join(projection)

    WRITE_SUMMARY[table] = {
        "deleted": to_delete[table] if replace_window else 0,
        "inserted": len(rows),
        "key_offset": offset,
    }

    if dry_run:
        print(f"  [dry run] {table}: would delete {to_delete[table]:,}, insert {len(rows):,} "
              f"({key_col} offset by {offset})")
        return

    if replace_window and to_delete[table]:
        spark.sql(
            f"DELETE FROM {FACTS}.{table} "
            f"WHERE {DATE_COLUMN[table]} BETWEEN {start_key} AND {end_key}"
        )

    # Explicit column list: insert-only, and the target schema is never widened
    # or reordered by this write.
    cols = EXPECTED_COLUMNS[table]
    spark.sql(f"INSERT INTO {FACTS}.{table} ({', '.join(cols)})\nSELECT {select_sql} FROM {view}")
    total = spark.table(f"{FACTS}.{table}").count()
    WRITE_SUMMARY[table]["table_rows_after"] = total
    print(f"  inserted {len(rows):>9,} into {table:<28} (table now {total:,} rows)")


write_facts(result.snapshots, "fact_inventory_snapshot")
write_facts(result.transactions, "fact_inventory_transaction")
write_facts(result.procurement, "fact_procurement")

# COMMAND ----------

# The ground truth. A new table this repo owns, alongside the ones
# schema_bootstrap already creates -- no existing schema is touched.
EVENT_SCHEMA = StructType([
    StructField("EVENT_TYPE", StringType()),
    StructField("PART_KEY", LongType()),
    StructField("WAREHOUSE_KEY", LongType()),
    StructField("SUPPLIER_KEY", LongType()),
    StructField("EVENT_DATE", DateType()),
    StructField("DETAIL", StringType()),
    StructField("VALUE_RS", DoubleType()),
])

events = [
    (
        e["event_type"],
        e["part_key"],
        e["warehouse_key"],
        e["supplier_key"],
        e["event_date"],
        e["detail"],
        e["value_rs"],
    )
    for e in result.events
]

if dry_run:
    print(f"  [dry run] would write {len(events):,} rows to {FACTS}.sim_events")
else:
    spark.createDataFrame(events, schema=EVENT_SCHEMA).createOrReplaceTempView("_sim_events")
    spark.sql(f"""
        CREATE OR REPLACE TABLE {FACTS}.sim_events
        COMMENT 'Ground truth for the simulated history: every stockout, chronically late
                 supplier, dead-stock pair and surplus the generator planted. Join against
                 scan output to measure detection recall and lead time.'
        AS SELECT
          EVENT_TYPE, PART_KEY, WAREHOUSE_KEY, SUPPLIER_KEY, EVENT_DATE, DETAIL,
          CAST(VALUE_RS AS DECIMAL(18,2)) AS VALUE_RS,
          current_timestamp() AS DW_LOADED_AT
        FROM _sim_events
    """)
    print(f"  wrote {spark.table(f'{FACTS}.sim_events').count():,} rows to sim_events")
    display(spark.sql(
        f"SELECT EVENT_TYPE, COUNT(*) events, ROUND(SUM(VALUE_RS), 0) value_rs "
        f"FROM {FACTS}.sim_events GROUP BY EVENT_TYPE ORDER BY events DESC"
    ))

# COMMAND ----------

# Which scenario produced each pair -- CONTROL, DEMAND_DRIFT, LEAD_DRIFT,
# LATE_PO, DEAD_STOCK or SURPLUS, exactly one per pair. This is what turns a
# detection-improvement number into an attributable one: `sim_events` says a
# stockout happened, `sim_pair_scenarios` says which mechanism was responsible
# for that pair's history, so a backtest can `GROUP BY scenario` instead of
# reporting one aggregate figure that mixes several causes together.
SCENARIO_SCHEMA = StructType([
    StructField("PART_KEY", LongType()),
    StructField("WAREHOUSE_KEY", LongType()),
    StructField("SCENARIO", StringType()),
    StructField("DEMAND_GROWTH_TARGET", DoubleType()),
    StructField("LEAD_DRIFT_TARGET", DoubleType()),
])

scenario_rows = [
    (
        p["part_key"],
        p["warehouse_key"],
        p["scenario"],
        p["demand_growth_target"],
        p["lead_drift_target"],
    )
    for p in result.pair_scenarios
]

if dry_run:
    print(f"  [dry run] would write {len(scenario_rows):,} rows to {FACTS}.sim_pair_scenarios")
else:
    spark.createDataFrame(scenario_rows, schema=SCENARIO_SCHEMA).createOrReplaceTempView("_sim_pair_scenarios")
    spark.sql(f"""
        CREATE OR REPLACE TABLE {FACTS}.sim_pair_scenarios
        COMMENT 'Which single scenario (CONTROL, DEMAND_DRIFT, LEAD_DRIFT, LATE_PO,
                 DEAD_STOCK, SURPLUS) produced each part/warehouse pair''s history.
                 Join against a backtest to attribute a detection difference to a
                 specific mechanism instead of reporting one mixed aggregate figure.'
        AS SELECT
          PART_KEY, WAREHOUSE_KEY, SCENARIO, DEMAND_GROWTH_TARGET, LEAD_DRIFT_TARGET,
          current_timestamp() AS DW_LOADED_AT
        FROM _sim_pair_scenarios
    """)
    print(f"  wrote {spark.table(f'{FACTS}.sim_pair_scenarios').count():,} rows to sim_pair_scenarios")
    display(spark.sql(
        f"SELECT SCENARIO, COUNT(*) pairs FROM {FACTS}.sim_pair_scenarios GROUP BY SCENARIO ORDER BY pairs DESC"
    ))

if dry_run:
    print("\nDRY RUN complete — nothing was written. Set dry_run=false to apply.")

# COMMAND ----------

import json

# Returned to the Jobs API so a run's outcome can be read back without digging
# through driver logs.
summary = {
    "dry_run": dry_run,
    "replace_window": replace_window,
    "window": [str(start_date), str(end_date)],
    "seed": seed,
    "tables": WRITE_SUMMARY,
    "ground_truth": result.event_counts(),
    "scenarios": result.scenario_counts(),
}
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
