"""Single source of truth for Unity Catalog locations and table names.

Two data domains, two owners:

- **Data Engineering's star schema** (`gold_dev.dim` / `gold_dev.supply_chain_analytics`)
  — dimension and fact tables we only ever *read* (inventory snapshots,
  inventory transactions, procurement, restock requests) except for
  `fact_restock_request`, which the Supervisor/Restock Agents also *write*
  quote/fulfillment lines into.
- **Our own schema** (`gold_dev.supply_chain_analytics`, same location as
  Data Engineering's facts schema — the old `ab_training.agentic_restock`
  schema this used to point at is gone) — governed artifacts we own outright:
  the phase-1 Unity Catalog functions and `quote_metadata`, a thin companion
  table holding the Teams/Review-App fields (`summary_report`,
  `teams_message_id`, `databricks_preview_url`, ...) that have no home in
  Data Engineering's `fact_restock_request` (grain: one row per requested
  part-line, not one row per quote header). `CATALOG`/`SCHEMA` stay separate
  config knobs from `GOLD_CATALOG`/`FACTS_SCHEMA` because the *ownership*
  distinction is still real even though the location no longer differs.

The Lakeflow trigger job, Supervisor/Genie/Restock agents, and the review app
all import from here instead of hardcoding catalog/schema/table names, so
re-pointing at a different environment (or a future schema revision) is a
one-place change (or an env var override, no code change at all).
"""

import os

# --- Data Engineering's star schema (read-mostly) ---------------------------

GOLD_CATALOG = os.environ.get("AGENTIC_RESTOCK_GOLD_CATALOG", "gold_dev")
DIM_SCHEMA = os.environ.get("AGENTIC_RESTOCK_DIM_SCHEMA", "dim")
FACTS_SCHEMA = os.environ.get("AGENTIC_RESTOCK_FACTS_SCHEMA", "supply_chain_analytics")

TABLE_DIM_PART = "dim_part"
TABLE_DIM_WAREHOUSE = "dim_warehouse"
TABLE_DIM_SUPPLIER = "dim_supplier"
TABLE_DIM_PLANT = "dim_plant"
TABLE_DIM_REQUEST_STATUS = "dim_request_status"

TABLE_FACT_INVENTORY_SNAPSHOT = "fact_inventory_snapshot"
TABLE_FACT_INVENTORY_TRANSACTION = "fact_inventory_transaction"
TABLE_FACT_PROCUREMENT = "fact_procurement"
TABLE_FACT_RESTOCK_REQUEST = "fact_restock_request"

# --- Application tables (quote metadata, BOM, contracts, capacity) --------

CATALOG = os.environ.get("AGENTIC_RESTOCK_CATALOG", "gold_dev")
SCHEMA = os.environ.get("AGENTIC_RESTOCK_SCHEMA", "supply_chain_analytics")

TABLE_QUOTE_METADATA = "quote_metadata"
TABLE_BOM = "dim_bom"
TABLE_SUPPLIER_CONTRACT = "dim_supplier_contract"
TABLE_PLANT_CAPACITY = "fact_plant_capacity"


def qualified_dim_table(table_name: str, catalog: str | None = None, schema: str | None = None) -> str:
    """Return the fully qualified `catalog.schema.table` name for a Data Engineering dimension table.

    Defaults to `GOLD_CATALOG`/`DIM_SCHEMA`; pass explicit `catalog`/`schema`
    to override both (e.g. a job parameter override).
    """
    return f"{catalog or GOLD_CATALOG}.{schema or DIM_SCHEMA}.{table_name}"


def qualified_fact_table(table_name: str, catalog: str | None = None, schema: str | None = None) -> str:
    """Return the fully qualified `catalog.schema.table` name for a Data Engineering fact table.

    Defaults to `GOLD_CATALOG`/`FACTS_SCHEMA`; pass explicit `catalog`/`schema`
    to override both (e.g. a job parameter override).
    """
    return f"{catalog or GOLD_CATALOG}.{schema or FACTS_SCHEMA}.{table_name}"


def qualified_table(table_name: str, catalog: str | None = None, schema: str | None = None) -> str:
    """Return the fully qualified `catalog.schema.table` name for one of our own artifacts.

    Defaults to the module-level `CATALOG`/`SCHEMA`; pass explicit
    `catalog`/`schema` to override both (e.g. a job parameter override).
    """
    return f"{catalog or CATALOG}.{schema or SCHEMA}.{table_name}"
