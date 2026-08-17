"""Create (or update) the Agentic Restock Supervisor Agent (architecture §2).

Supervisor Agent has no native Databricks Asset Bundle resource type yet — the
Databricks SDK's `supervisor_agents` service is Beta and SDK-only (see
`docs/agent_bricks_mapping.md`). This script is the "as code" record of how
the Supervisor Agent and its tools were created, so it can be re-created (or
recreated after deletion) without re-deriving the configuration by hand.

Usage:
    python scripts/create_supervisor_agent.py --profile anurag-r \\
        --genie-space-id <space_id_from_resources/genie/genie_agent.genie_space.yml>

This is NOT idempotent — running it again creates a second Supervisor Agent.
If you need to update an existing one, use `w.supervisor_agents.update_tool`
/ `create_tool` / `delete_tool` directly (see the "Manage supervisor agents
using the Databricks SDK" section of the Agent Bricks docs).
"""

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.supervisoragents import GenieSpace, SupervisorAgent, Tool, UcFunction

CATALOG = "ab_training"
SCHEMA = "agentic_restock"

# §4.2 UC functions (see notebooks/uc_functions/deep_analysis_functions.ipynb)
UC_FUNCTION_TOOLS = {
    "avg_daily_consumption": (
        "Average daily consumption over a trailing window (default 14 days) "
        "for one item/warehouse."
    ),
    "predicted_stockout_date": (
        "Earliest predicted stockout date for one item/warehouse, projected from today."
    ),
    "classify_urgency": (
        "Classifies urgency (CRITICAL/HIGH/MEDIUM/LOW) given current stock, minimum "
        "stock, and days remaining until stockout."
    ),
    "requested_restock_qty": (
        "Suggested restock quantity (target_stock_qty - current_stock_qty, floored at "
        "0) for one item/warehouse."
    ),
    "needs_restock": ("Veto decision: whether a Lakeflow-flagged candidate genuinely needs restocking."),
    "restock_candidate_summary": (
        "Deterministic natural-language summary of one restock candidate (stock, "
        "consumption, forecast, urgency, suggested reorder qty)."
    ),
}

SUPERVISOR_DESCRIPTION = (
    "Supervisor Agent for the Agentic Restock workflow (architecture §2, §4). "
    "Coordinates the Agentic Restock Genie Agent (natural-language analysis over "
    "inventory, thresholds, and consumption history) and the §4.2 Unity Catalog "
    "functions (avg_daily_consumption, predicted_stockout_date, classify_urgency, "
    "requested_restock_qty, needs_restock, restock_candidate_summary) to triage "
    "low-stock candidates from the hourly Lakeflow trigger job, decide urgency, apply "
    "the restock veto, and produce a quote/summary for human approval."
)

SUPERVISOR_INSTRUCTIONS = (
    "You are invoked by the hourly Lakeflow trigger job with a list of item/warehouse "
    "candidates whose current stock is at or below their reorder point. For each "
    "candidate: 1) call needs_restock(item_id, warehouse_id) to apply the veto -- drop "
    "any candidate where this returns false; 2) for remaining candidates, call "
    "restock_candidate_summary(item_id, warehouse_id) and classify_urgency (via the "
    "Genie Agent or directly) to get the urgency level and a natural-language "
    "explanation; 3) synthesize a single response covering all candidates, ordered by "
    "urgency (CRITICAL first), including the suggested reorder quantity from "
    "requested_restock_qty for each. Always use the Unity Catalog functions or the "
    "Genie Agent for these calculations -- never estimate consumption trends, stockout "
    "dates, or urgency yourself."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="~/.databrickscfg profile to use")
    parser.add_argument(
        "--genie-space-id",
        required=True,
        help="Genie Agent space_id to attach as a tool (see resources/genie/genie_agent.genie_space.yml)",
    )
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    created = w.supervisor_agents.create_supervisor_agent(
        supervisor_agent=SupervisorAgent(
            display_name="Agentic Restock - Supervisor Agent",
            description=SUPERVISOR_DESCRIPTION,
            instructions=SUPERVISOR_INSTRUCTIONS,
        )
    )
    print(f"Created supervisor agent: {created.name} (endpoint: {created.endpoint_name})")

    parent = created.name

    w.supervisor_agents.create_tool(
        parent=parent,
        tool_id="genie_agent",
        tool=Tool(
            tool_type="genie_space",
            description=(
                "Agentic Restock Genie Agent -- natural language deep analysis over "
                "inventory_stock_level, threshold_config_table, and consumption_history, "
                "using the §4.2 Unity Catalog functions for consumption trend, stockout "
                "forecast, and urgency scoring."
            ),
            genie_space=GenieSpace(id=args.genie_space_id, space_id=args.genie_space_id),
        ),
    )
    print("Added tool: genie_agent")

    for fn_name, description in UC_FUNCTION_TOOLS.items():
        w.supervisor_agents.create_tool(
            parent=parent,
            tool_id=fn_name,
            tool=Tool(
                tool_type="uc_function",
                description=description,
                uc_function=UcFunction(name=f"{CATALOG}.{SCHEMA}.{fn_name}"),
            ),
        )
        print(f"Added tool: {fn_name}")

    print(f"\nSupervisor Agent ready. Endpoint: {created.endpoint_name}")
    print("Grant end users CAN QUERY on the endpoint, and EXECUTE/access on each subagent")
    print("(see 'Supported subagents and tools' in the Agent Bricks docs).")


if __name__ == "__main__":
    main()
