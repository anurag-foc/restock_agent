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
    "supply chain analysis. Ask it natural-language questions and it will query governed Unity "
    "Catalog functions across 4 intelligence layers:\n"
    "Layer 1 (Forecast & Signal Validation): Computes true burn rates, seasonality-adjusted "
    "consumption forecasts, predicted stockout dates, consumption anomaly detection, and "
    "per-supplier dynamic reorder points.\n"
    "Layer 2 (Procurement Intelligence): Evaluates whether a restock signal is a false positive "
    "(open PO veto), checks inter-warehouse network surplus for lateral transfers before "
    "recommending external POs, ranks suppliers by composite reliability score, and adjusts "
    "ideal quantities to feasible MOQ/pack-size increments.\n"
    "Layer 3 (Manufacturing Constraints): Explodes finished-good demand into BOM component "
    "requirements, identifies constraining bottleneck components and production value at risk, "
    "and validates production volume against rated plant capacity.\n"
    "Layer 4 (Financial Decision Framing): Contrasts stockout-driven production loss against "
    "excess MOQ carrying cost to frame every recommendation as a quantified business decision.\n"
    "Always ask this tool rather than estimating values — it is the single source of governed "
    "computation and multi-dimensional supply chain reasoning."
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
    "## Signal-Aware Routing Rules\n"
    "Candidates flagged by the Lakeflow multi-signal scanner contain a `signal_type`:\n"
    "- `STOCK_THRESHOLD`: Current stock has breached safety stock. Execute full 4-layer protocol immediately.\n"
    "- `PREDICTED_STOCKOUT`: Proactive burn-rate signal — part will stockout within lead-time window. Lead with Layer 1 (burn rate) and Layer 2 (transfer vs PO to prevent stockout).\n"
    "- `BOM_CASCADE_RISK`: Component shortfall threatens a critical assembly. Lead with Layer 3 (BOM explosion & assembly risk) first, then resolve component procurement in Layer 2.\n\n"
    "## Core Reasoning Protocol\n"
    "For every inventory signal or restocking question, reason through these layers in order. "
    "Ask the Genie Agent scoped natural-language questions for each layer.\n\n"
    "### 1. VALIDATE THE SIGNAL (Is this real demand?)\n"
    "   - Ask Genie for the consumption burn rate, seasonality forecast, and anomaly score.\n"
    "   - If the anomaly z-score is elevated, pause and advise the PM to verify the consumption "
    "data before proceeding. Do not auto-generate a quote on suspicious signals.\n"
    "   - Compute the dynamic reorder point under both preferred and fallback supplier lead times. "
    "Surface both scenarios when lead-time variance exceeds 5 days.\n\n"
    "### 2. EVALUATE PROCUREMENT OPTIONS (Transfer first, PO second)\n"
    "   - Ask Genie to compare requested_restock_qty against pending_procurement_qty. If open POs "
    "already cover the shortfall, declare it a false positive and explain why.\n"
    "   - Ask Genie for network_surplus. If another warehouse has transferable surplus, present "
    "internal transfer as Option A (faster, zero procurement cost) before any external PO.\n"
    "   - When an external PO is needed, ask Genie to rank suppliers by reliability score and "
    "adjust the shortfall to a feasible order quantity (MOQ & pack size constraints).\n\n"
    "### 3. ASSESS MANUFACTURING IMPACT (Does this affect assembly?)\n"
    "   - For finished-good parts, ask Genie to explode the BOM and identify component shortfalls.\n"
    "   - Ask for the assembly risk report to find the constraining bottleneck — one missing ₹50 "
    "component can halt production of a ₹50,000 assembly.\n"
    "   - Ask Genie to check plant capacity against the required production volume. Surface any "
    "capacity gap and overflow options.\n\n"
    "### 4. FRAME THE DECISION FINANCIALLY (₹, not units)\n"
    "   - Ask Genie for the financial tradeoff summary. Express the cost of inaction (production "
    "halt value) against the cost of action (excess MOQ holding cost).\n"
    "   - When the cost of inaction vastly exceeds the overstock cost, state the recommendation "
    "plainly.\n\n"
    "## Synthesis Rules\n"
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
