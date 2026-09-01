"""POC demo dataset — one deliberate scenario per phase-1 intelligence nuance.

This is not a statistical simulator. The goal stated for the POC is to show
the system's capability, not its behaviour at scale, so instead of generating
noisy history and hoping each of the seven nuances happens to fire somewhere
in it, this hand-designs one clear "hero" pair per nuance, using real part /
warehouse / supplier IDs already in gold_dev (never invented ones — that is
what broke dim_bom / dim_supplier_contract before this).

Note: `notebooks/simulation/generate_sim_data.py` imports
`agentic_restock.simulation.SimulationConfig` / `simulate`, and that module
does not exist in this repo. Whatever produced the current ~68K snapshot rows
either ran from a version of that file that was never committed, or ran
directly in the workspace. This module does not depend on it and does not
try to repair it — a 550-day statistical generator is the wrong tool for a
POC demo anyway.

Design, one row per nuance (docs/market_evidence_phase1.md §7):

  1. Network surplus / transfer
     PRT-022 (ABS Sensor Module) short at WH001, surplus at WH005.
     Donor stays well clear of its own safety stock after the transfer.

  2. BOM cascade -> value at risk
     PRT-033 (Steering Rack Assembly, A-CRITICAL) needs 2x PRT-037 (Wheel Hub
     Bearing) per unit. The component looks healthy on its own (above its
     own safety stock) but is short of what the parent's build target needs.

  3. Decision-value ranking (validated once rank_priority_actions exists)
     P1009 (Dual Front Airbag Module) near-zero at WH001, single-sourced,
     75-day lead time. Stocked at only three warehouses total, and the other
     two are deliberately set to exactly their own safety stock -- zero
     donor surplus, not just "less surplus than the shortfall needs". Large
     face-value exposure, ~zero decision value once the ranking function
     exists, because nothing cheap fixes it. This is a genuine "hopeless"
     case to rank against the cheap-fix cases above -- an earlier attempt
     using PRT-031 for this role turned out to have 3,826 units of real
     surplus sitting at another warehouse, which made it a transfer story
     instead. PRT-031 is kept as that (a second, larger transfer case) and
     P1009 takes over the hopeless-case role.

  4. Seasonality-adjusted consumption
     PRT-029 (Fuel Injector Nozzle) at WH003: a flat trailing average says
     ~10 units/day (safe against on-hand); the last 30 days actually ran at
     ~35/day (production ramp). The board's seasonal multiplier should catch
     what the flat snapshot average misses.

  5 & 6. Lead-time drift + supplier reliability
     PRT-024 (Rear Axle Shaft) contracted to SUP005 (10-day lead, chronically
     ~12 days late, poor OTD). SUP001 offered as the uncontracted alternative
     with clean delivery history and a slightly higher price — a genuine
     cost/reliability tradeoff for evaluate_suppliers to surface.

  7. MOQ / pack feasibility
     PRT-040 (Rear View Camera Module) at WH004: ideal shortfall is 120
     units, but SUP008's contract has MOQ 1000 / pack 250 — a stark
     "the honest number and the executable number are very different" case.

All snapshot changes are INSERTS of a new dated row, never mutations of
history — the board's `latest_snapshot` CTE already takes the most recent
`SNAPSHOT_DATE_KEY` per part/warehouse, so a fresh row for today becomes
"latest" without touching anything the existing 68K rows represent.
"""

import datetime as dt

from agentic_restock.config import qualified_fact_table, qualified_table

TABLE_FACT_INVENTORY_SNAPSHOT = "fact_inventory_snapshot"
TABLE_FACT_INVENTORY_TRANSACTION = "fact_inventory_transaction"
TABLE_FACT_SUPPLIER_DELIVERY = "fact_supplier_delivery"
TABLE_BOM = "dim_bom"
TABLE_SUPPLIER_CONTRACT = "dim_supplier_contract"

# Real part/warehouse/supplier keys and IDs pulled from gold_dev (2026-09-01).
# PART_KEY -> PART_ID
HERO_PARTS = {
    9: "P1009",     # Dual Front Airbag Module     (sub-assembly,  nuance 3 -- hopeless case)
    22: "PRT-022",  # ABS Sensor Module            (component,     nuance 1)
    24: "PRT-024",  # Rear Axle Shaft              (component,     nuance 5/6)
    29: "PRT-029",  # Fuel Injector Nozzle         (component,     nuance 4)
    31: "PRT-031",  # ECU Engine Control Unit      (sub-assembly,  nuance 1 -- large stakes)
    33: "PRT-033",  # Steering Rack Assembly       (assembly,      nuance 2 parent)
    37: "PRT-037",  # Wheel Hub Bearing            (component,     nuance 2 component)
    40: "PRT-040",  # Rear View Camera Module      (component,     nuance 7)
}
WH001, WH002, WH003, WH004, WH005, WH016, WH024 = 1, 2, 3, 4, 5, 16, 24
SUP_SUZUKI, SUP_AISIN, SUP_MINDA, SUP_NIPPON = 1, 5, 8, 6  # SUP001, SUP005, SUP008, SUP006


def build_dim_bom_statement(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    """Replace dim_bom with real-ID rows. Small and deliberate: one A-CRITICAL
    parent (PRT-033) with one component (PRT-037), sized so the component
    looks healthy alone but is short of the parent's build target.
    """
    bom = qualified_table(TABLE_BOM, app_catalog, app_schema)
    return f"""
    CREATE OR REPLACE TABLE {bom} (
      fg_part_id STRING NOT NULL COMMENT 'Finished good or assembly part ID (matches dim_part.PART_ID)',
      component_part_id STRING NOT NULL COMMENT 'Child component part ID required for assembly (matches dim_part.PART_ID)',
      qty_per_unit INT NOT NULL COMMENT 'Quantity of component required per single unit of finished good',
      created_at TIMESTAMP COMMENT 'Row creation timestamp',
      CONSTRAINT pk_dim_bom_demo PRIMARY KEY (fg_part_id, component_part_id)
    )
    COMMENT 'Bill of Materials — POC demo rows against real dim_part IDs (docs/market_evidence_phase1.md nuance 2).';

    INSERT INTO {bom} VALUES
      ('PRT-033', 'PRT-037', 2, current_timestamp());
    """.strip()


def build_dim_supplier_contract_statement(app_catalog: str | None = None, app_schema: str | None = None) -> str:
    """Replace dim_supplier_contract with real-ID rows covering every hero part.

    PRT-024 gets two contracts (the contracted-but-late SUP005, and the
    clean-but-pricier SUP001) so evaluate_suppliers has a real tradeoff to
    surface. PRT-031 gets exactly one, single-sourced, long lead time — part
    of what makes it the "hopeless" case. PRT-040's MOQ/pack is set to be
    dramatically larger than its shortfall, on purpose.
    """
    contract = qualified_table(TABLE_SUPPLIER_CONTRACT, app_catalog, app_schema)
    return f"""
    CREATE OR REPLACE TABLE {contract} (
      part_id STRING NOT NULL COMMENT 'Part business key matching dim_part.PART_ID',
      supplier_id STRING NOT NULL COMMENT 'Supplier business key matching dim_supplier.SUPPLIER_ID',
      lead_time_days INT NOT NULL COMMENT 'Contracted lead time in days for this part-supplier pair',
      moq INT NOT NULL COMMENT 'Minimum Order Quantity (MOQ) required by supplier',
      pack_size INT NOT NULL COMMENT 'Pack size increment (order quantity must be multiple of pack_size)',
      unit_cost DOUBLE COMMENT 'Contracted unit price in INR',
      is_preferred BOOLEAN COMMENT 'True if this is the primary contract supplier',
      created_at TIMESTAMP COMMENT 'Row creation timestamp',
      CONSTRAINT pk_dim_supplier_contract_demo PRIMARY KEY (part_id, supplier_id)
    )
    COMMENT 'Supplier contract terms — POC demo rows against real dim_part/dim_supplier IDs.';

    INSERT INTO {contract} VALUES
      -- Nuance 5/6: contracted supplier (Aisin) is chronically late; the
      -- uncontracted alternative (Suzuki Powertrain) is clean but pricier.
      ('PRT-024', 'SUP005', 10, 200, 50,  3100.00, true,  current_timestamp()),
      ('PRT-024', 'SUP001',  9, 200, 50,  3250.00, false, current_timestamp()),
      -- Nuance 3: single-sourced, long lead time, and (see the snapshot
      -- rows) genuinely no surplus at either of this part's two other
      -- warehouses -- part of what makes this the "hopeless" case for
      -- decision-value ranking to correctly demote below the cheap fixes.
      ('P1009', 'SUP006', 75, 50, 10, 17800.00, true, current_timestamp()),
      -- PRT-031: single-sourced too, but turned out to have real network
      -- surplus elsewhere (see snapshot rows) -- kept as a second, larger
      -- transfer case rather than the hopeless one.
      ('PRT-031', 'SUP003', 60, 50,  10,  11400.00, true, current_timestamp()),
      -- Nuance 2: the cascade component needs a contract too, for
      -- evaluate_feasibility once it is picked as a resolution option.
      ('PRT-037', 'SUP004', 14, 300, 50,  1400.00, true,  current_timestamp()),
      -- Nuance 7: MOQ/pack dramatically larger than the ideal shortfall.
      ('PRT-040', 'SUP008', 18, 1000, 250, 2050.00, true, current_timestamp());
    """.strip()


def build_snapshot_upsert_statement(
    key_offset: int,
    gold_catalog: str | None = None,
    facts_schema: str | None = None,
    as_of: dt.date | None = None,
) -> str:
    """Insert one fresh dated row per hero (part, warehouse) — never mutates
    existing history. `key_offset` must be `SELECT MAX(INVENTORY_SNAPSHOT_KEY)
    FROM <table>` read by the caller first: Databricks SQL rejects a scalar
    subquery inside a VALUES clause, so the offset can't be computed inline.
    """
    snapshot = qualified_fact_table(TABLE_FACT_INVENTORY_SNAPSHOT, gold_catalog, facts_schema)
    date_key = int((as_of or dt.datetime.now(dt.UTC).date()).strftime("%Y%m%d"))

    # Unit costs pulled from dim_part directly (2026-09-01) rather than joined
    # at insert time, so this statement has no cross-table subquery to get wrong.
    unit_cost = {9: 18500.00, 22: 890.50, 24: 3150.00, 29: 1200.00, 31: 12000.00, 33: 6800.00, 37: 1450.00, 40: 2100.00}

    # (part_key, warehouse_key, on_hand, safety_stock, max_stock, avg_daily_consumption, stockout_risk)
    rows = [
        # Nuance 3 (hopeless case): P1009 near-zero at WH001; its other two
        # stocking warehouses set to EXACTLY their own safety stock, so
        # donor_surplus_qty is genuinely 0 at both -- not just "less than
        # the shortfall needs". No cheap fix exists for this one.
        (9, WH001, 8, 200, 390, 3.0, "HIGH"),
        (9, WH016, 62, 62, 489, 2.5, "LOW"),
        (9, WH024, 52, 52, 278, 2.0, "LOW"),
        # Nuance 1: PRT-022 short at WH001, healthy surplus at WH005.
        (22, WH001, 40, 150, 400, 4.0, "HIGH"),
        (22, WH005, 520, 140, 600, 3.0, "LOW"),
        # Nuance 2: parent short on its own account too (real-world signals
        # usually stack); component healthy alone, short for the build target.
        (33, WH002, 10, 15, 60, 1.5, "HIGH"),
        (37, WH002, 60, 45, 250, 5.0, "LOW"),
        # PRT-031: severe shortage with real network surplus elsewhere in the
        # existing data -- a second, larger-stakes transfer case (not the
        # hopeless one; see the contract comment above).
        (31, WH003, 5, 200, 250, 3.0, "HIGH"),
        # Nuance 4: flat average says safe (400 vs safety 350); the last 30
        # days of transactions inserted below say otherwise.
        (29, WH003, 400, 350, 900, 10.0, "LOW"),
        # Nuance 5/6: no shortfall needed here -- the lead-time/reliability
        # signal comes entirely from the contract + delivery history.
        (24, WH001, 300, 120, 500, 6.0, "LOW"),
        # Nuance 7: modest shortfall that a 1000-unit MOQ will dwarf.
        (40, WH004, 30, 100, 150, 2.0, "HIGH"),
    ]

    values_sql = ",\n      ".join(
        f"({key_offset + i + 1}, "
        f"{date_key}, {part_key}, {wh_key}, {on_hand}, {safety}, {maxs}, "
        f"0, {on_hand}, 0, 0, "
        f"CAST({round(on_hand / adc, 1)} AS DECIMAL(6,1)), CAST({adc} AS DECIMAL(10,2)), "
        f"CAST(0.0 AS DECIMAL(6,2)), CAST({round(on_hand * unit_cost[part_key], 2)} AS DECIMAL(16,2)), "
        f"'{risk}', current_timestamp())"
        for i, (part_key, wh_key, on_hand, safety, maxs, adc, risk) in enumerate(rows)
    )

    return f"""
    INSERT INTO {snapshot} (
      INVENTORY_SNAPSHOT_KEY, SNAPSHOT_DATE_KEY, PART_KEY, WAREHOUSE_KEY,
      QUANTITY_ON_HAND, SAFETY_STOCK_QTY, MAX_STOCK_LEVEL, ALLOCATED_QTY,
      AVAILABLE_QTY, IN_TRANSIT_QTY, BLOCKED_QTY, DAYS_OF_SUPPLY,
      AVG_DAILY_CONSUMPTION, INVENTORY_TURNOVER_RATIO, STOCK_VALUATION,
      STOCKOUT_RISK, DW_LOADED_AT
    )
    VALUES
      {values_sql}
    """.strip()


def build_seasonality_transaction_statement(
    key_offset: int,
    gold_catalog: str | None = None,
    facts_schema: str | None = None,
    as_of: dt.date | None = None,
) -> str:
    """Insert daily ISSUE history for PRT-029 @ WH003: ~10/day baseline for
    days 400..31 ago, ramping to ~35/day for the last 30 days. History spans
    >90 days so the board's seasonality guard rail (MIN_HISTORY_DAYS_FOR_SEASONALITY)
    trusts the multiplier rather than falling back to the flat average.
    `key_offset` must be `SELECT MAX(INVENTORY_TXN_KEY) FROM <table>` read by
    the caller first (see build_snapshot_upsert_statement docstring).
    """
    txn = qualified_fact_table(TABLE_FACT_INVENTORY_TRANSACTION, gold_catalog, facts_schema)
    end = as_of or dt.datetime.now(dt.UTC).date()

    rows = []
    for days_ago in range(1, 401):
        day = end - dt.timedelta(days=days_ago)
        qty = 35 if days_ago <= 30 else 10
        date_key = int(day.strftime("%Y%m%d"))
        rows.append((date_key, qty))

    values_sql = ",\n      ".join(
        f"({key_offset + i + 1}, "
        f"'DEMO-TXN-PRT029-{i + 1:04d}', {date_key}, 29, {WH003}, "
        f"NULL, NULL, NULL, 'ISSUE', {qty}, 1200.00, "
        f"CAST({qty * 1200.00} AS DECIMAL(16,2)), NULL, current_timestamp())"
        for i, (date_key, qty) in enumerate(rows)
    )

    return f"""
    INSERT INTO {txn} (
      INVENTORY_TXN_KEY, TRANSACTION_ID, TRANSACTION_DATE_KEY, PART_KEY,
      WAREHOUSE_KEY, PRODUCTION_ORDER_ID, LINE_KEY, OPERATOR_EMPLOYEE_KEY,
      TRANSACTION_TYPE, QUANTITY, UNIT_COST, TRANSACTION_VALUE,
      BALANCE_AFTER_TXN, DW_LOADED_AT
    )
    VALUES
      {values_sql}
    """.strip()


def build_supplier_delivery_statement(
    key_offset: int,
    gold_catalog: str | None = None,
    facts_schema: str | None = None,
    as_of: dt.date | None = None,
) -> str:
    """Insert delivery history for SUP005 (chronically late) and SUP001
    (clean) against PRT-024 @ WH001, so nuance 5/6 have real observed data
    to compute from instead of the contracted lead time alone. `key_offset`
    must be `SELECT MAX(SUPPLIER_DELIVERY_KEY) FROM <table>` read by the
    caller first (see build_snapshot_upsert_statement docstring).
    """
    delivery = qualified_fact_table(TABLE_FACT_SUPPLIER_DELIVERY, gold_catalog, facts_schema)
    end = as_of or dt.datetime.now(dt.UTC).date()

    # (supplier_key, delay_days, otd_flag) x 6 deliveries each, spread over the last ~180 days
    aisin_deliveries = [(SUP_AISIN, 14, False), (SUP_AISIN, 9, False), (SUP_AISIN, 15, False),
                        (SUP_AISIN, 11, False), (SUP_AISIN, 13, False), (SUP_AISIN, 10, False)]
    suzuki_deliveries = [(SUP_SUZUKI, 0, True), (SUP_SUZUKI, 1, True), (SUP_SUZUKI, -1, True),
                         (SUP_SUZUKI, 0, True), (SUP_SUZUKI, 1, True), (SUP_SUZUKI, 0, True)]

    all_deliveries = aisin_deliveries + suzuki_deliveries
    rows = []
    for i, (supplier_key, delay, otd) in enumerate(all_deliveries):
        planned_day = end - dt.timedelta(days=180 - (i * 25))
        delivered_day = planned_day + dt.timedelta(days=max(delay, 0))
        rows.append((supplier_key, int(planned_day.strftime("%Y%m%d")), int(delivered_day.strftime("%Y%m%d")), delay, otd))

    values_sql = ",\n      ".join(
        f"({key_offset + i + 1}, "
        f"'DEMO-DLV-PRT024-{i + 1:03d}', {delivered_key}, {planned_key}, {supplier_key}, {WH001}, "
        f"200, {delay}, 0, 0, 3500.00, {str(otd).upper()}, "
        f"'{'ON_TIME' if otd else 'DELAYED'}', current_timestamp())"
        for i, (supplier_key, planned_key, delivered_key, delay, otd) in enumerate(rows)
    )

    return f"""
    INSERT INTO {delivery} (
      SUPPLIER_DELIVERY_KEY, DELIVERY_ID, DELIVERY_DATE_KEY, PLANNED_DATE_KEY,
      SUPPLIER_KEY, WAREHOUSE_KEY, QUANTITY, DELAY_DAYS, DAMAGED_QTY,
      SHORT_QTY, FREIGHT_COST, OTD_FLAG, DELIVERY_STATUS, DW_LOADED_AT
    )
    VALUES
      {values_sql}
    """.strip()
