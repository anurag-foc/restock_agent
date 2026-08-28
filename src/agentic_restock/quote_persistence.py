"""Quote Persistence Module — Writes multi-tier Supervisor quotes to Delta tables.

Persists enriched quotes into:
  - `gold_dev.supply_chain_analytics.fact_restock_request` (fact line items)
  - `gold_dev.supply_chain_analytics.quote_metadata` (markdown report & tracking)
"""

import datetime
import uuid
import json


def generate_quote_id() -> str:
    """Generate a unique quote ID string (e.g. QT-20260828-A1B2)."""
    today_str = datetime.date.today().strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:4].upper()
    return f"QT-{today_str}-{short_uuid}"


def build_insert_quote_metadata_sql(
    quote_id: str,
    summary_report: str,
    teams_message_id: str | None = None,
    preview_url: str | None = None,
    catalog: str = "gold_dev",
    schema: str = "supply_chain_analytics",
) -> str:
    """Construct SQL statement to insert a record into quote_metadata."""
    # Escape single quotes for SQL insertion
    clean_report = summary_report.replace("'", "''")
    clean_teams_id = f"'{teams_message_id}'" if teams_message_id else "NULL"
    clean_url = f"'{preview_url}'" if preview_url else "NULL"

    return f"""
        INSERT INTO {catalog}.{schema}.quote_metadata (
            quote_id,
            summary_report,
            teams_message_id,
            teams_sent_at,
            databricks_preview_url,
            created_by,
            created_at,
            updated_at
        ) VALUES (
            '{quote_id}',
            '{clean_report}',
            {clean_teams_id},
            { 'current_timestamp()' if teams_message_id else 'NULL' },
            {clean_url},
            'supervisor_agent',
            current_timestamp(),
            current_timestamp()
        )
    """.strip()


def build_insert_fact_restock_request_sql(
    quote_id: str,
    candidates: list[dict],
    catalog: str = "gold_dev",
    dim_schema: str = "dim",
    facts_schema: str = "supply_chain_analytics",
) -> str:
    """Construct SQL statement to insert line items into fact_restock_request."""
    today_key = int(datetime.date.today().strftime("%Y%m%d"))
    values_clauses = []

    for idx, c in enumerate(candidates, 1):
        item_id = c.get("item_id") or c.get("PART_ID")
        wh_id = c.get("warehouse_id") or c.get("WAREHOUSE_ID")
        req_id = f"REQ-{quote_id.replace('QT-', '')}-{idx}"

        curr_stock = int(c.get("current_stock_qty", 0))
        reorder_pt = int(c.get("reorder_point_qty", 0))
        suggested_qty = int(c.get("suggested_reorder_qty", 0))

        clause = f"""
            SELECT
                '{quote_id}' AS QUOTE_ID,
                '{req_id}' AS RESTOCK_REQUEST_ID,
                {today_key} AS REQUESTED_DATE_KEY,
                dp.PART_KEY,
                dw.WAREHOUSE_KEY,
                1 AS REQUEST_STATUS_KEY, -- Default PENDING_APPROVAL
                {curr_stock} AS CURRENT_STOCK_QTY,
                {reorder_pt} AS REORDER_POINT_QTY,
                {suggested_qty} AS REQUESTED_QTY,
                {suggested_qty} AS CONFIRMED_QTY,
                0 AS VARIANCE_QTY,
                current_timestamp() AS DW_LOADED_AT
            FROM {catalog}.{dim_schema}.dim_part dp
            CROSS JOIN {catalog}.{dim_schema}.dim_warehouse dw
            WHERE dp.PART_ID = '{item_id}' AND dp.IS_CURRENT = true
              AND dw.WAREHOUSE_ID = '{wh_id}'
        """.strip()
        values_clauses.append(clause)

    combined_select = "\nUNION ALL\n".join(values_clauses)

    return f"""
        INSERT INTO {catalog}.{facts_schema}.fact_restock_request (
            RESTOCK_REQUEST_KEY,
            QUOTE_ID,
            RESTOCK_REQUEST_ID,
            REQUESTED_DATE_KEY,
            PART_KEY,
            WAREHOUSE_KEY,
            REQUEST_STATUS_KEY,
            CURRENT_STOCK_QTY,
            REORDER_POINT_QTY,
            REQUESTED_QTY,
            CONFIRMED_QTY,
            VARIANCE_QTY,
            DW_LOADED_AT
        )
        SELECT
            (SELECT COALESCE(MAX(RESTOCK_REQUEST_KEY), 0) FROM {catalog}.{facts_schema}.fact_restock_request) + ROW_NUMBER() OVER (ORDER BY QUOTE_ID) AS RESTOCK_REQUEST_KEY,
            QUOTE_ID, RESTOCK_REQUEST_ID, REQUESTED_DATE_KEY,
            PART_KEY, WAREHOUSE_KEY, REQUEST_STATUS_KEY,
            CURRENT_STOCK_QTY, REORDER_POINT_QTY, REQUESTED_QTY, CONFIRMED_QTY, VARIANCE_QTY,
            DW_LOADED_AT
        FROM (
            {combined_select}
        )
    """.strip()


def persist_quote(
    candidates: list[dict],
    supervisor_response_text: str,
    spark=None,
    workspace_client=None,
    catalog: str = "gold_dev",
    dim_schema: str = "dim",
    facts_schema: str = "supply_chain_analytics",
    warehouse_id: str = "d2533a75c1bd9265",
) -> str:
    """Persist a quote and its line items to Delta tables.

    Returns the generated quote_id.
    """
    quote_id = generate_quote_id()

    sql_meta = build_insert_quote_metadata_sql(
        quote_id=quote_id,
        summary_report=supervisor_response_text,
        catalog=catalog,
        schema=facts_schema,
    )

    sql_fact = build_insert_fact_restock_request_sql(
        quote_id=quote_id,
        candidates=candidates,
        catalog=catalog,
        dim_schema=dim_schema,
        facts_schema=facts_schema,
    )

    if spark is not None:
        spark.sql(sql_meta)
        spark.sql(sql_fact)
    elif workspace_client is not None:
        workspace_client.statement_execution.execute_statement(
            statement=sql_meta, warehouse_id=warehouse_id, wait_timeout="30s"
        )
        workspace_client.statement_execution.execute_statement(
            statement=sql_fact, warehouse_id=warehouse_id, wait_timeout="30s"
        )
    else:
        raise ValueError("Either spark or workspace_client must be provided to persist_quote.")

    return quote_id
