"""End-to-End Pipeline Execution Script.

Runs the complete agentic pipeline:
1. Scans gold_dev via multi-signal scanner (Signals S1, S2, S3)
2. Invokes Supervisor Agent endpoint (mas-486e7d15-endpoint) with candidate payload
3. Persists Quote & line items to fact_restock_request and quote_metadata Delta tables
4. Dispatches real Teams Adaptive Card to TEAMS_WEBHOOK_URL
5. Updates quote_metadata with teams_message_id, teams_sent_at, and databricks_preview_url

Usage:
    export TEAMS_WEBHOOK_URL="https://your-org.webhook.office.com/..."
    PYTHONPATH=src python3 scripts/run_e2e_pipeline.py
"""

import json
import os
import sys

sys.path.insert(0, "src")

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config
from agentic_restock.jobs.lakeflow_trigger import build_coarse_check_query
from agentic_restock.quote_persistence import persist_quote
from agentic_restock.integrations.teams_webhook import (
    build_review_app_url,
    send_quote_card,
)


def main() -> None:
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        print("❌ Error: TEAMS_WEBHOOK_URL environment variable is required for end-to-end testing.")
        print("   Please set it before running: export TEAMS_WEBHOOK_URL='https://...'")
        sys.exit(1)

    print("🚀 Starting End-to-End Pipeline Test...")
    print(f"   Target Webhook: {webhook_url[:45]}...")

    w = WorkspaceClient(profile="anurag-r", config=Config(http_timeout_seconds=600, retry_timeout_seconds=900))
    warehouse_id = "d2533a75c1bd9265"

    # Step 1: Run Multi-Signal Scanner
    print("\n1️⃣ Running Multi-Signal Scanner query on gold_dev...")
    query = build_coarse_check_query()
    res = w.statement_execution.execute_statement(statement=query, warehouse_id=warehouse_id, wait_timeout="30s")

    if not res.result or not res.result.data_array:
        print("⚠️ No supply chain candidates found by scanner.")
        sys.exit(0)

    cols = [c.name for c in res.manifest.schema.columns]
    candidates = [dict(zip(cols, row)) for row in res.result.data_array]
    print(f"   Found {len(candidates)} candidate(s):")
    for c in candidates:
        print(f"   - [{c.get('signal_type')} | {c.get('initial_urgency')}] {c.get('item_id')} @ {c.get('warehouse_id')}")

    # Step 2: Invoke Supervisor Agent Endpoint
    prompt = (
        "The Lakeflow multi-signal agentic scanner flagged the following supply chain candidates. "
        "Each candidate includes its specific signal_type (STOCK_THRESHOLD, PREDICTED_STOCKOUT, or BOM_CASCADE_RISK). "
        "Route each candidate through your 4-layer reasoning protocol (Forecast Validation -> Procurement Intelligence -> Manufacturing Constraints -> Financial Framing). "
        "Apply the restock veto, surface lateral transfer vs PO options, explode BOM components if applicable, "
        "and produce a prioritized intelligence quote (CRITICAL first):\n\n" + json.dumps(candidates, default=str)
    )

    print("\n2️⃣ Invoking Supervisor Agent Endpoint (mas-486e7d15-endpoint)...")
    endpoint_name = "mas-486e7d15-endpoint"
    resp = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint_name}/invocations",
        body={"input": [{"role": "user", "content": prompt}]},
    )

    final_text = ""
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    final_text = part["text"]

    if not final_text:
        print("⚠️ Supervisor returned raw response without message text.")
        final_text = json.dumps(resp, indent=2)

    print("\n=== Supervisor Intelligence Report ===")
    print(final_text)

    # Step 3: Persist Quote & Line Items to Delta Tables
    print("\n3️⃣ Persisting Restock Quote to Delta tables...")
    quote_id = persist_quote(
        candidates=candidates,
        supervisor_response_text=final_text,
        workspace_client=w,
        warehouse_id=warehouse_id,
    )
    print(f"   Saved Quote ID: {quote_id} in fact_restock_request and quote_metadata.")

    # Step 4: Dispatch Teams Adaptive Card
    print("\n4️⃣ Dispatching Teams Adaptive Card notification...")
    workspace_url = os.environ.get("DATABRICKS_HOST", "https://adb-4321.azuredatabricks.net")
    review_url = build_review_app_url(quote_id=quote_id, workspace_url=workspace_url)

    teams_result = send_quote_card(
        quote_id=quote_id,
        candidates=candidates,
        supervisor_summary=final_text,
        review_app_url=review_url,
        webhook_url=webhook_url,
        dry_run=False,
    )

    teams_msg_id = teams_result.get("teams_message_id")
    print(f"   Teams Card Sent! Message ID: {teams_msg_id}")

    # Step 5: Update quote_metadata with tracking info
    print("\n5️⃣ Updating quote_metadata with Teams dispatch tracking...")
    sql_update = f"""
        UPDATE gold_dev.supply_chain_analytics.quote_metadata
        SET teams_message_id = '{teams_msg_id}',
            teams_sent_at = current_timestamp(),
            databricks_preview_url = '{review_url}',
            updated_at = current_timestamp()
        WHERE quote_id = '{quote_id}'
    """
    w.statement_execution.execute_statement(statement=sql_update, warehouse_id=warehouse_id, wait_timeout="30s")

    print("\n🎉 End-to-End Pipeline Execution Completed Successfully!")
    print(f"   - Quote ID: {quote_id}")
    print(f"   - Review Link: {review_url}")
    print("   - Check your Microsoft Teams channel for the incoming Adaptive Card!")


if __name__ == "__main__":
    main()
