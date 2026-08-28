"""Lakeflow trigger job: the §4.1 coarse low-stock check.

A cheap indexed join between the latest row of `fact_inventory_snapshot`
(Data Engineering's star schema) and its `dim_part`/`dim_warehouse` dimension
tables. This is the *only* thing that runs every hour — it returns one row
per part/warehouse currently at or below its safety-stock (reorder) level.
The expensive analysis (consumption trend, stockout forecast, urgency
scoring, the restock veto) belongs to the Genie Agent / §4.2 Unity Catalog
functions and only ever runs against whatever this query returns, never
against the full fact table.

Schema note: Data Engineering's `fact_inventory_snapshot` has no `is_active`/
`reorder_point_qty` config row like the old mock `threshold_config_table` —
those concepts are folded directly into the snapshot fact
(`SAFETY_STOCK_QTY` is the reorder trigger, `MAX_STOCK_LEVEL` is the restock
target), and it's a *daily snapshot* fact (one row per part x warehouse x
day), not a single current-state row, so we always take the most recent
`SNAPSHOT_DATE_KEY` per part/warehouse via `ROW_NUMBER()`.

Kept as a plain function (no Spark import) so it's unit-testable without a
Databricks runtime. The notebook that actually executes it lives at
`notebooks/lakeflow_trigger/coarse_check.py`.
"""

from agentic_restock.config import (
    TABLE_DIM_PART,
    TABLE_DIM_WAREHOUSE,
    TABLE_FACT_INVENTORY_SNAPSHOT,
    qualified_dim_table,
    qualified_fact_table,
)


def build_coarse_check_query(
    gold_catalog: str | None = None,
    dim_schema: str | None = None,
    facts_schema: str | None = None,
) -> str:
    """Return the §4.1 multi-signal agentic scanner SQL.

    Defaults to `agentic_restock.config`'s `GOLD_CATALOG`/`DIM_SCHEMA`/
    `FACTS_SCHEMA`; pass explicit overrides to target a different location
    (e.g. a job parameter override) without touching this function.
    """
    snapshot_table = qualified_fact_table(TABLE_FACT_INVENTORY_SNAPSHOT, gold_catalog, facts_schema)
    part_table = qualified_dim_table(TABLE_DIM_PART, gold_catalog, dim_schema)
    warehouse_table = qualified_dim_table(TABLE_DIM_WAREHOUSE, gold_catalog, dim_schema)

    # Functions schema defaults to supply_chain_analytics
    cat = gold_catalog or "gold_dev"
    sch = facts_schema or "supply_chain_analytics"
    func_prefix = f"{cat}.{sch}"

    return f"""
        WITH latest_snapshot AS (
          SELECT
            *,
            ROW_NUMBER() OVER (
              PARTITION BY PART_KEY, WAREHOUSE_KEY
              ORDER BY SNAPSHOT_DATE_KEY DESC
            ) AS rn
          FROM {snapshot_table}
        ),
        active_stock AS (
          SELECT
            dp.PART_ID AS item_id,
            dp.PART_NAME AS item_name,
            dw.WAREHOUSE_ID AS warehouse_id,
            ls.QUANTITY_ON_HAND AS current_stock_qty,
            ls.SAFETY_STOCK_QTY AS reorder_point_qty,
            ls.MAX_STOCK_LEVEL AS target_stock_qty,
            (ls.MAX_STOCK_LEVEL - ls.QUANTITY_ON_HAND) AS suggested_reorder_qty,
            ls.STOCKOUT_RISK AS stockout_risk
          FROM latest_snapshot ls
          JOIN {part_table} dp
            ON ls.PART_KEY = dp.PART_KEY AND dp.IS_CURRENT = true
          JOIN {warehouse_table} dw
            ON ls.WAREHOUSE_KEY = dw.WAREHOUSE_KEY
          WHERE ls.rn = 1
            AND dp.LIFECYCLE_STATUS = 'ACTIVE'
            AND dw.OPERATIONAL_STATUS = 'ACTIVE'
        ),
        -- Signal 1: Stock Threshold Breached (Reactive)
        s1_threshold AS (
          SELECT
            item_id, item_name, warehouse_id,
            current_stock_qty, reorder_point_qty, target_stock_qty, suggested_reorder_qty, stockout_risk,
            'STOCK_THRESHOLD' AS signal_type,
            'CRITICAL' AS initial_urgency,
            CAST(NULL AS INT) AS days_to_stockout,
            CAST(NULL AS STRING) AS threatened_assembly
          FROM active_stock
          WHERE current_stock_qty <= reorder_point_qty
        ),
        -- Signal 2: Predictive Stockout (Proactive burn-rate scan)
        s2_predictive AS (
          SELECT
            item_id, item_name, warehouse_id,
            current_stock_qty, reorder_point_qty, target_stock_qty, suggested_reorder_qty, stockout_risk,
            'PREDICTED_STOCKOUT' AS signal_type,
            'HIGH' AS initial_urgency,
            DATEDIFF(
              {func_prefix}.predicted_stockout_date(item_id, warehouse_id),
              CURRENT_DATE()
            ) AS days_to_stockout,
            CAST(NULL AS STRING) AS threatened_assembly
          FROM active_stock
          WHERE current_stock_qty > reorder_point_qty
            AND {func_prefix}.predicted_stockout_date(item_id, warehouse_id) IS NOT NULL
            AND {func_prefix}.predicted_stockout_date(item_id, warehouse_id) <= DATE_ADD(CURRENT_DATE(), 14)
        ),
        -- Signal 3: BOM Cascade Risk (Component shortfall for critical assemblies)
        s3_bom AS (
          SELECT
            s.item_id, s.item_name, s.warehouse_id,
            s.current_stock_qty, s.reorder_point_qty, s.target_stock_qty, s.suggested_reorder_qty, s.stockout_risk,
            'BOM_CASCADE_RISK' AS signal_type,
            'HIGH' AS initial_urgency,
            CAST(NULL AS INT) AS days_to_stockout,
            bom.FG_PART_ID AS threatened_assembly
          FROM active_stock s
          JOIN {func_prefix}.dim_bom bom ON s.item_id = bom.COMPONENT_PART_ID
          JOIN {part_table} fg ON bom.FG_PART_ID = fg.PART_ID AND fg.IS_CURRENT = true
          WHERE s.current_stock_qty > s.reorder_point_qty
            AND fg.PART_TYPE IN ('ASSEMBLY', 'SUB-ASSEMBLY')
            AND (s.current_stock_qty - bom.QTY_PER_UNIT * 100) < 0
        ),
        combined_signals AS (
          SELECT * FROM s1_threshold
          UNION ALL
          SELECT * FROM s2_predictive
          UNION ALL
          SELECT * FROM s3_bom
        ),
        deduped_signals AS (
          SELECT *,
            ROW_NUMBER() OVER (
              PARTITION BY item_id, warehouse_id
              ORDER BY CASE initial_urgency WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END
            ) AS signal_rank
          FROM combined_signals
        )
        SELECT
          item_id, item_name, warehouse_id,
          current_stock_qty, reorder_point_qty, target_stock_qty, suggested_reorder_qty, stockout_risk,
          signal_type, initial_urgency, days_to_stockout, threatened_assembly
        FROM deduped_signals
        WHERE signal_rank = 1
        ORDER BY
          CASE initial_urgency WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END,
          (current_stock_qty * 1.0 / NULLIF(reorder_point_qty, 0)) ASC
    """.strip()
