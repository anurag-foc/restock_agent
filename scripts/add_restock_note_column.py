"""One-off migration: add a NOTE column to fact_restock_request.

fact_restock_request lives in Data Engineering's star schema and its DDL is
not otherwise tracked in this repo (see CLAUDE.md), so this is an ALTER TABLE
run directly against the live warehouse rather than a CREATE OR REPLACE in
schema_bootstrap. NOTE holds the PM's free-text reasoning for an
approve/reject decision on a part-line, entered in the restock-review app.

Idempotent: checks information_schema before altering, safe to re-run.

Usage:
    PYTHONPATH=src python3 scripts/add_restock_note_column.py --profile anurag-r
"""

import argparse
import sys

sys.path.insert(0, "src")

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem
from agentic_restock.config import qualified_fact_table, TABLE_FACT_RESTOCK_REQUEST

WAREHOUSE_ID = "d2533a75c1bd9265"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)
    table = qualified_fact_table(TABLE_FACT_RESTOCK_REQUEST)
    catalog, schema, table_name = table.split(".")

    existing = w.statement_execution.execute_statement(
        statement=(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = :catalog AND table_schema = :schema "
            "AND table_name = :table_name AND column_name = 'NOTE'"
        ),
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="30s",
        parameters=[
            StatementParameterListItem(name="catalog", value=catalog),
            StatementParameterListItem(name="schema", value=schema),
            StatementParameterListItem(name="table_name", value=table_name),
        ],
    )
    if existing.result and existing.result.data_array:
        print(f"{table}.NOTE already exists -- nothing to do.")
        return

    print(f"Adding NOTE column to {table}...")
    w.statement_execution.execute_statement(
        statement=(
            f"ALTER TABLE {table} ADD COLUMNS "
            "(NOTE STRING COMMENT 'PM free-text reasoning for the approve/reject decision on this line')"
        ),
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="30s",
    )
    print("Done.")


if __name__ == "__main__":
    main()
