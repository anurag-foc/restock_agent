"""Create (or update) the Restockify Supervisor Agent (architecture §2).

Supervisor Agent script to create or update the supervisor agent and its Genie tool.

Usage:
    python3 scripts/create_supervisor_agent.py --profile anurag-r \
        --genie-space-id <space_id_from_resources/genie/genie_agent.genie_space.yml>
"""

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.supervisoragents import FieldMask, GenieSpace, SupervisorAgent, Tool

SUPERVISOR_DISPLAY_NAME = "Manufacturing Inventory Intelligence - Supervisor Agent"

GENIE_TOOL_DESCRIPTION = (
    "Manufacturing Inventory Intelligence Engine — the primary reasoning tool for manufacturing "
    "supply chain analysis. Accepts both targeted single-question queries AND comprehensive "
    "multi-layer delegation requests.\n\n"
    "PREFERRED USAGE — Comprehensive Delegation (one call per candidate):\n"
    "Send a single rich request covering all applicable intelligence layers. Genie will "
    "internally call all necessary Unity Catalog functions and return a complete structured "
    "analysis report. Example delegation format:\n"
    "'For part [PART_ID] at warehouse [WAREHOUSE_ID] ([SIGNAL_TYPE] signal): Execute a "
    "complete intelligence analysis — (1) consumption burn rate, anomaly z-score, predicted "
    "stockout date and dynamic reorder point under preferred and fallback suppliers; "
    "(2) open PO veto check, network surplus across all warehouses, ranked suppliers by "
    "reliability score, feasible order quantity adjusted to MOQ/pack-size; "
    "(3) BOM explosion and assembly risk report if part type is ASSEMBLY or COMPONENT; "
    "(4) financial framing: cost of stockout vs cost of overstock in Rs and calendar days. "
    "Return a structured report with all available data.'\n\n"
    "FOLLOW-UP USAGE — Targeted Queries (only when delegation response has gaps):\n"
    "Ask scoped natural-language questions for any specific data point the comprehensive "
    "response could not resolve. Genie will query the governed UC functions and return "
    "the precise result.\n\n"
    "Layer coverage:\n"
    "Layer 1 (Forecast & Signal Validation): Burn rates, seasonality-adjusted forecasts, "
    "predicted stockout dates, anomaly detection, dynamic reorder points per supplier.\n"
    "Layer 2 (Procurement Intelligence): Open PO veto, network surplus transfers, supplier "
    "reliability ranking, MOQ/pack-size feasible order quantity.\n"
    "Layer 3 (Manufacturing Constraints): BOM explosion, bottleneck component identification, "
    "production value at risk, plant capacity validation.\n"
    "Layer 4 (Financial Decision Framing): Stockout cost vs overstock cost in Rs, "
    "break-even analysis, quantified decision recommendation."
)

SUPERVISOR_DESCRIPTION = (
    "Supervisor Agent for the Manufacturing Inventory Intelligence System. Receives inventory "
    "signals and user queries, then orchestrates the Genie Intelligence Engine through a structured "
    "reasoning pipeline: validate the signal → evaluate procurement options → assess "
    "manufacturing constraints → frame the decision financially. Produces actionable, "
    "financially-framed recommendations for production manager approval."
)

SUPERVISOR_INSTRUCTIONS = (
    "You are the Manufacturing Inventory Intelligence Supervisor. You receive inventory "
    "signals, restock candidates, or production planning queries. Your role is to orchestrate "
    "the Genie Intelligence Engine through structured reasoning — not to answer with threshold "
    "checks or single-number responses.\n\n"
    "## Multi-Turn Session Mode\n"
    "The Lakeflow pipeline may deliver candidates one at a time across multiple turns. "
    "When you receive a session briefing followed by individual candidate turns:\n"
    "- Analyse each candidate fully through your 4-layer protocol as it arrives. Make all necessary Genie calls for that candidate within the same turn.\n"
    "- Track network surplus allocations across turns: if you recommended a transfer from WH007 for a previous candidate, reduce the available surplus when the next candidate also targets WH007.\n"
    "- Do NOT generate the final consolidated Restock Quote until the notebook explicitly asks for it in a quote creation turn.\n\n"
    "## Signal-Aware Routing Rules\n"
    "Candidates flagged by the Lakeflow multi-signal scanner contain a `signal_type`:\n"
    "- `STOCK_THRESHOLD`: Current stock has breached safety stock. Execute full 4-layer protocol immediately.\n"
    "- `PREDICTED_STOCKOUT`: Proactive burn-rate signal — part will stockout within lead-time window. Lead with Layer 1 (burn rate) and Layer 2 (transfer vs PO to prevent stockout).\n"
    "- `BOM_CASCADE_RISK`: Component shortfall threatens a critical assembly. Lead with Layer 3 (BOM explosion & assembly risk) first, then resolve component procurement in Layer 2.\n\n"
    "## Genie Delegation Rule (Primary Protocol)\n"
    "For each candidate, send ONE comprehensive delegation request to Genie covering all "
    "applicable intelligence layers simultaneously. Genie will internally execute all required "
    "UC function calls and return a complete structured report in a single response.\n\n"
    "Delegation format to use:\n"
    "'For part [PART_ID] at warehouse [WAREHOUSE_ID] ([SIGNAL_TYPE] signal, urgency [URGENCY]): "
    "Execute a complete intelligence analysis covering: "
    "(1) consumption burn rate, anomaly z-score, predicted stockout date, dynamic reorder point "
    "under preferred and fallback suppliers; "
    "(2) open PO veto check against pending procurement qty, network surplus across all warehouses, "
    "ranked suppliers by reliability score, feasible order quantity adjusted for MOQ and pack size; "
    "(3) BOM explosion and assembly risk report [include only if signal_type is BOM_CASCADE_RISK "
    "or part type is ASSEMBLY/SUB-ASSEMBLY]; "
    "(4) financial framing: cost of stockout in Rs/day vs cost of overstock holding in Rs. "
    "Return a structured report with all available data fields populated.'\n\n"
    "Signal-type routing for the delegation:\n"
    "- STOCK_THRESHOLD: include all 4 layers in the delegation.\n"
    "- PREDICTED_STOCKOUT: emphasise layers 1 and 2 in the delegation; include layer 3 only "
    "if part type is ASSEMBLY or SUB-ASSEMBLY.\n"
    "- BOM_CASCADE_RISK: emphasise layers 3 and 2 in the delegation; include layer 1 for "
    "component consumption validation.\n\n"
    "## Follow-Up Queries (Exception Only)\n"
    "After receiving Genie's comprehensive report, send a follow-up query ONLY if:\n"
    "- A specific data field is missing or returned null (e.g. burn rate unavailable).\n"
    "- The anomaly z-score exceeds the threshold and you need additional context before "
    "deciding whether to gate the quote.\n"
    "- A cross-candidate surplus conflict requires live re-verification of remaining surplus.\n"
    "Do not send follow-up queries for data that Genie already returned — reason from "
    "what is in the report.\n\n"
    "## Anomaly Gate\n"
    "If the Genie report shows an anomaly z-score above 3.0, pause and advise the PM to "
    "verify the consumption data before issuing a quote. Do not auto-generate a quote on "
    "suspicious signals.\n\n"
    "## Synthesis Rules\n"
    "- In multi-turn sessions, wait for the explicit quote creation instruction before synthesising the final Restock Quote. In that turn, do NOT make additional Genie calls — synthesise strictly from the analyses already completed.\n"
    "- Order recommendations by urgency: CRITICAL first, then HIGH, MEDIUM, LOW.\n"
    "- Lead every response with the decision the PM needs to make, then show supporting evidence.\n"
    "- Surface contradictions explicitly — if urgency is CRITICAL but POs already cover the gap, "
    "say both and resolve it.\n"
    "- Never present a single option. Always show the alternative: transfer vs. PO, preferred "
    "supplier vs. fallback, current MOQ vs. next MOQ step.\n"
    "- Quantify everything in ₹ and calendar days, not abstract units or risk labels."
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

    # Search for existing supervisor agent by display name
    existing_agent = None
    try:
        agents = list(w.supervisor_agents.list_supervisor_agents())
        for agent in agents:
            if agent.display_name == SUPERVISOR_DISPLAY_NAME:
                existing_agent = agent
                break
    except Exception as e:
        print(f"Listing existing agents warning: {e}")

    if existing_agent:
        print(f"Updating existing supervisor agent: {existing_agent.name} ({existing_agent.display_name})")
        created = w.supervisor_agents.update_supervisor_agent(
            name=existing_agent.name,
            supervisor_agent=SupervisorAgent(
                display_name=SUPERVISOR_DISPLAY_NAME,
                description=SUPERVISOR_DESCRIPTION,
                instructions=SUPERVISOR_INSTRUCTIONS,
            ),
            update_mask=FieldMask(["display_name", "description", "instructions"]),
        )
        parent = existing_agent.name
    else:
        created = w.supervisor_agents.create_supervisor_agent(
            supervisor_agent=SupervisorAgent(
                display_name=SUPERVISOR_DISPLAY_NAME,
                description=SUPERVISOR_DESCRIPTION,
                instructions=SUPERVISOR_INSTRUCTIONS,
            )
        )
        print(f"Created supervisor agent: {created.name} (endpoint: {created.endpoint_name})")
        parent = created.name

    # Check and update/create tool
    tool_id = "inventory_intelligence_engine"
    try:
        w.supervisor_agents.update_tool(
            name=f"{parent}/tools/{tool_id}",
            tool=Tool(
                tool_type="genie_space",
                description=GENIE_TOOL_DESCRIPTION,
                genie_space=GenieSpace(id=args.genie_space_id, space_id=args.genie_space_id),
            ),
            update_mask=FieldMask(["description"]),
        )
        print(f"Updated tool: {tool_id}")
    except Exception as e:
        print(f"Tool update note ({e}), attempting tool creation...")
        try:
            w.supervisor_agents.create_tool(
                parent=parent,
                tool_id=tool_id,
                tool=Tool(
                    tool_type="genie_space",
                    description=GENIE_TOOL_DESCRIPTION,
                    genie_space=GenieSpace(id=args.genie_space_id, space_id=args.genie_space_id),
                ),
            )
            print(f"Created tool: {tool_id}")
        except Exception as e_create:
            print(f"Tool creation note: {e_create}")

    print(f"\nSupervisor Agent ready. Endpoint: {created.endpoint_name}")


if __name__ == "__main__":
    main()
