"""Lakeflow trigger job: the §4.1 coarse low-stock check.

A cheap indexed join between `inventory_stock_level` and `threshold_config_table`.
This is the *only* thing that runs every hour — it returns one row per
item/warehouse currently at or below its reorder point. The expensive analysis
(consumption trend, stockout forecast, urgency scoring) belongs to the Genie
Agent and only ever runs against whatever this query returns, never against
the full table.

Kept as a plain function (no Spark import) so it's unit-testable without a
Databricks runtime. The notebook that actually executes it lives at
`notebooks/lakeflow_trigger/coarse_check.py`.
"""

from agentic_restock.config import (
    TABLE_INVENTORY_STOCK_LEVEL,
    TABLE_THRESHOLD_CONFIG,
    qualified_table,
)


def build_coarse_check_query(catalog: str | None = None, schema: str | None = None) -> str:
    """Return the §4.1 coarse low-stock check SQL.

    Defaults to `agentic_restock.config`'s catalog/schema; pass explicit
    `catalog`/`schema` to target a different location (e.g. a job parameter
    override) without touching this function.
    """
    inventory_table = qualified_table(TABLE_INVENTORY_STOCK_LEVEL, catalog, schema)
    threshold_table = qualified_table(TABLE_THRESHOLD_CONFIG, catalog, schema)

    return f"""
        SELECT
          isl.item_id,
          isl.item_name,
          isl.warehouse_id,
          isl.current_stock_qty,
          isl.unit_of_measure,
          tct.reorder_point_qty,
          tct.minimum_stock_qty,
          tct.target_stock_qty,
          (tct.target_stock_qty - isl.current_stock_qty) AS suggested_reorder_qty,
          tct.lead_time_days
        FROM {inventory_table} isl
        JOIN {threshold_table} tct
          ON isl.item_id = tct.item_id AND isl.warehouse_id = tct.warehouse_id
        WHERE tct.is_active = true
          AND isl.current_stock_qty <= tct.reorder_point_qty
        ORDER BY (isl.current_stock_qty * 1.0 / tct.reorder_point_qty) ASC
    """.strip()
