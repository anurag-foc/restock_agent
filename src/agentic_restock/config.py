"""Single source of truth for the Unity Catalog location and table names.

The Lakeflow trigger job, Supervisor/Genie/Restock agents, and the review
app all import from here instead of hardcoding `ab_training.agentic_restock`,
so re-pointing the whole pipeline at real Data Engineering tables later is a
one-place change (or an env var override, no code change at all).
"""

import os

CATALOG = os.environ.get("AGENTIC_RESTOCK_CATALOG", "ab_training")
SCHEMA = os.environ.get("AGENTIC_RESTOCK_SCHEMA", "agentic_restock")

TABLE_INVENTORY_STOCK_LEVEL = "inventory_stock_level"
TABLE_THRESHOLD_CONFIG = "threshold_config_table"
TABLE_CONSUMPTION_HISTORY = "consumption_history"
TABLE_OPEN_REQUEST = "open_request"
TABLE_RESTOCK_REQUESTS = "restock_requests"


def qualified_table(table_name: str, catalog: str | None = None, schema: str | None = None) -> str:
    """Return the fully qualified `catalog.schema.table` name.

    Defaults to the module-level `CATALOG`/`SCHEMA`; pass explicit
    `catalog`/`schema` to override both (e.g. a job parameter override).
    """
    return f"{catalog or CATALOG}.{schema or SCHEMA}.{table_name}"
