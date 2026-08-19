"""Create (or update) the Restockify Supervisor Agent (architecture §2).

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

Deliberately a single-tool Supervisor: the only tool attached is `genie_agent`
(a `genie_space` tool). The §4.2 Unity Catalog functions (avg_daily_consumption,
predicted_stockout_date, classify_urgency, requested_restock_qty,
pending_procurement_qty, open_procurement_orders, restock_candidate_summary,
avg_lead_time_days, latest_snapshot) are trusted assets on the Genie Space
itself (see `notebooks/genie/genie_agent.geniespace.json`), NOT attached here
as direct `uc_function` tools on the Supervisor. Earlier revisions of this
script attached them to both, which let the Supervisor bypass Genie and call
the analytics functions directly for candidates where it already had exact
part_id/warehouse_id -- observed doing exactly that (calling the old
`needs_restock` and `restock_candidate_summary` functions straight from the
Lakeflow hand-off, never invoking `genie_agent`). Removing direct access
forces every analysis question (veto, urgency, forecast, suggested reorder
qty) through the Genie Agent, so all §4.2 deep-analysis logic has one single
entry point.
"""

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.supervisoragents import GenieSpace, SupervisorAgent, Tool

SUPERVISOR_DISPLAY_NAME = "Restockify - Supervisor Agent"

GENIE_TOOL_DESCRIPTION = (
    "Restockify Genie Agent -- natural language deep analysis over Data "
    "Engineering's gold_dev star schema (fact_inventory_snapshot, "
    "fact_inventory_transaction, fact_procurement, fact_restock_request), using "
    "the §4.2 Unity Catalog functions (avg_daily_consumption, "
    "predicted_stockout_date, classify_urgency, requested_restock_qty, "
    "pending_procurement_qty, open_procurement_orders, "
    "restock_candidate_summary, avg_lead_time_days, latest_snapshot) for "
    "consumption trend, stockout forecast, urgency scoring, the restock veto "
    "(computed by comparing requested_restock_qty against "
    "pending_procurement_qty, not a single yes/no function), and "
    "natural-language candidate summaries. This is the ONLY way to get any "
    "of that analysis -- ask it questions rather than computing anything "
    "yourself. Scoped to pre-quote analysis only; quote_metadata is out of scope."
)

SUPERVISOR_DESCRIPTION = (
    "Supervisor Agent for the Restockify workflow (architecture §2, §4). "
    "Coordinates the Restockify Genie Agent -- its one and only tool -- to "
    "triage low-stock candidates from the hourly Lakeflow trigger job, decide "
    "urgency, apply the restock veto, and produce a quote/summary for human "
    "approval. Holds no direct access to the §4.2 Unity Catalog analytics "
    "functions; all analysis is delegated to the Genie Agent via natural "
    "language questions."
)

SUPERVISOR_INSTRUCTIONS = (
    "You are invoked by the hourly Lakeflow trigger job with a list of part/warehouse "
    "candidates (part_id, warehouse_id business keys) whose current stock is at or "
    "below their safety-stock reorder point. You have exactly one tool: the Genie "
    "Agent. You do NOT have direct access to any analytics function (no "
    "classify_urgency, requested_restock_qty, pending_procurement_qty, "
    "restock_candidate_summary, etc.) -- all of that logic lives behind the Genie "
    "Agent, and it must be reached "
    "by asking Genie a natural-language question, never by calling a function "
    "yourself. For each candidate (or a batch of candidates in one question), ask "
    "the Genie Agent: (1) whether it genuinely needs restocking (the veto -- drop any "
    "candidate Genie says does not); (2) its urgency level; (3) its suggested reorder "
    "quantity; (4) a natural-language explanation you can reuse in the summary. Once "
    "you have Genie's analysis for every candidate, synthesize a single response "
    "ordered by urgency (CRITICAL first), including the suggested reorder quantity for "
    "each, suitable for a Teams notification. Never estimate consumption trends, "
    "stockout dates, urgency, or veto decisions yourself -- if you don't have an answer "
    "from Genie yet, ask Genie, don't guess."
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
            display_name=SUPERVISOR_DISPLAY_NAME,
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
            description=GENIE_TOOL_DESCRIPTION,
            genie_space=GenieSpace(id=args.genie_space_id, space_id=args.genie_space_id),
        ),
    )
    print("Added tool: genie_agent")

    print(f"\nSupervisor Agent ready. Endpoint: {created.endpoint_name}")
    print("Grant end users CAN QUERY on the endpoint, and EXECUTE/access on each subagent")
    print("(see 'Supported subagents and tools' in the Agent Bricks docs).")


if __name__ == "__main__":
    main()
