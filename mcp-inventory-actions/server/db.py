"""SQL execution helper for the Inventory Intelligence action tools.

Uses the app's own service principal identity (default `WorkspaceClient()`
auth, auto-detected from the Databricks Apps runtime) rather than
on-behalf-of-user auth — the caller here is the Supervisor Agent, not an
interactive user. The app's service principal needs explicit Unity Catalog
grants on the tables below; see docs/agent_bricks_mapping.md.
"""

import datetime
import os

from databricks.sdk.service.sql import StatementParameterListItem, StatementState

from server import utils

CATALOG = os.environ.get("GOLD_CATALOG", "gold_dev")
DIM_SCHEMA = os.environ.get("GOLD_DIM_SCHEMA", "dim")
FACTS_SCHEMA = os.environ.get("GOLD_FACTS_SCHEMA", "supply_chain_analytics")

FACT_RESTOCK_REQUEST = f"{CATALOG}.{FACTS_SCHEMA}.fact_restock_request"
FACT_INVENTORY_SNAPSHOT = f"{CATALOG}.{FACTS_SCHEMA}.fact_inventory_snapshot"
QUOTE_METADATA = f"{CATALOG}.{FACTS_SCHEMA}.quote_metadata"
DIM_REQUEST_STATUS = f"{CATALOG}.{DIM_SCHEMA}.dim_request_status"
DIM_PART = f"{CATALOG}.{DIM_SCHEMA}.dim_part"
DIM_WAREHOUSE = f"{CATALOG}.{DIM_SCHEMA}.dim_warehouse"


def run_sql(statement: str, parameters: list[StatementParameterListItem] | None = None) -> list[list]:
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not warehouse_id:
        raise ValueError("DATABRICKS_WAREHOUSE_ID is not configured")

    w = utils.get_workspace_client()
    resp = w.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=warehouse_id,
        wait_timeout="50s",
        parameters=parameters or [],
    )
    if resp.status is None or resp.status.state != StatementState.SUCCEEDED:
        error_message = resp.status.error.message if resp.status and resp.status.error else "unknown error"
        raise RuntimeError(f"SQL {resp.status.state if resp.status else 'UNKNOWN'}: {error_message}")
    return resp.result.data_array if resp.result and resp.result.data_array else []


def param(name: str, value, type_: str = "STRING") -> StatementParameterListItem:
    return StatementParameterListItem(name=name, type=type_, value=str(value))


def today_date_key() -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    return int(now.strftime("%Y%m%d"))
