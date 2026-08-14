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


def qualified_table(table_name: str) -> str:
    """Return the fully qualified `catalog.schema.table` name."""
    return f"{CATALOG}.{SCHEMA}.{table_name}"
