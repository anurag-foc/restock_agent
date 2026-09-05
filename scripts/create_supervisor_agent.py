"""Create (or update) the Inventory Intelligence Supervisor Agent (architecture §2).

Supervisor Agent script to create or update the supervisor agent and its Genie tool.

Usage:
    python3 scripts/create_supervisor_agent.py --profile anurag-r \
        --genie-space-id <space_id_from_resources/genie/genie_agent.genie_space.yml>
"""

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.supervisoragents import FieldMask, GenieSpace, SupervisorAgent, Tool

SUPERVISOR_DISPLAY_NAME = "Manufacturing Inventory Intelligence - Supervisor Agent"

GUARDRAIL_TOOL_DESCRIPTION = (
    "Fulfillment Guardrail — a fulfillment-time guardrail for a restock line a Production "
    "Manager has ALREADY APPROVED. Use this only during a fulfillment turn, never during quote "
    "creation.\n\n"
    "It exists to catch one specific failure: a request that sat PENDING_APPROVAL long enough "
    "that the stock situation already changed before someone approved it — replenished some "
    "other way, covered by a newer PO, demand collapsed. Ask it for a single verdict: PROCEED or "
    "NEEDS_REVIEW, with a short reason. It does NOT compute or propose a quantity — the "
    "fulfill_restock_request action tool reads live stock and works out CONFIRMED_QTY/"
    "VARIANCE_QTY itself; do not ask the Fulfillment Guardrail for a number and do not pass one "
    "along yourself.\n\n"
    "It is READ-ONLY: it recommends, it never writes. Record its verdict with the "
    "fulfill_restock_request action tool."
)

ACTIONS_TOOL_DESCRIPTION = (
    "Inventory Intelligence action tools — the only way to write to the warehouse or notify a human. "
    "Exposes three operations:\n\n"
    "1. `persist_quote(candidates_json, summary_report)` — save a finished quote to Delta as "
    "PENDING_APPROVAL (one header row plus one line per candidate). Returns the quote_id. Call "
    "this once, immediately after you produce a consolidated Restock Quote.\n"
    "2. `send_human_review(quote_id, summary_report, force_resend=False)` — send the "
    "Production Manager a Microsoft Teams card with a deep link to the Databricks Review App. Call "
    "this only after persist_quote has returned a quote_id, using that exact id. The Review App "
    "link is built server-side from the quote_id — do not invent or pass a review URL yourself. "
    "By default it no-ops if a card was already sent for that quote_id, so a retry never spams "
    "Teams. Only when a human explicitly asks you to resend/re-notify for a specific quote_id, "
    "call it again with force_resend=true — never set force_resend on a routine or retried call.\n"
    "3. `fulfill_restock_request(restock_request_key, proceed, note)` — record your PROCEED/"
    "NEEDS_REVIEW verdict on a single APPROVED line, after asking the Fulfillment Guardrail. This "
    "tool computes the confirmed quantity and variance itself from live data — you supply only "
    "the boolean verdict and a short note, never a quantity. Call this only during a fulfillment "
    "turn.\n\n"
    "All three are idempotent — a repeated call reports the existing state rather than "
    "duplicating a quote, a Teams card, or a status transition, unless you explicitly override "
    "send_human_review with force_resend=true."
)

GENIE_TOOL_DESCRIPTION = (
    "Manufacturing Inventory Intelligence Engine — the primary reasoning tool for manufacturing "
    "supply chain analysis. You are never handed a candidate list; you find out what needs "
    "attention by calling this tool yourself.\n\n"
    "PHASE-1 WORKFLOW (docs/market_evidence_phase1.md §7) — call in this order:\n"
    "1. 'Run the priority scan and tell me the top-ranked action.' -- internally calls "
    "rank_priority_actions, which reads a precomputed signal board (one row per part/warehouse, "
    "all seven phase-1 signals) and ranks by decision_value = exposure minus the cost of the "
    "cheapest viable fix, not raw exposure. A huge-exposure item nothing cheap fixes ranks BELOW "
    "a smaller item a transfer solves for almost nothing -- this is deliberate, not a bug to "
    "second-guess. A part/warehouse with an open restock request (pending, approved, or being "
    "fulfilled) is suppressed from this ranking WHILE that request is fresh, so it never shows up "
    "twice -- but if that request has sat too long (2+ days awaiting a PM decision, or past its "
    "own lead time in fulfillment), it resurfaces here with signal_type = 'STALLED_COMMITMENT' "
    "and a commitment_state/commitment_age_days column telling you what it is stuck in and for "
    "how long. This is not a new stock problem -- it is an existing one going unresolved. Do not "
    "propose a second transfer or PO for it; the response is to flag the stall itself (who/what "
    "it is waiting on, per commitment_state) so a human expedites or re-decides the existing "
    "request.\n"
    "2. Drill into the picked action with only the functions it actually needs: "
    "scan_transfer_options (donor-protected network surplus -- always the cheapest fix when one "
    "exists), scan_assembly_risk (does a healthy-looking component still threaten a critical "
    "assembly's build target), scan_demand_shift (has seasonally-adjusted burn diverged from the "
    "flat average), scan_leadtime_drift (has observed supplier delivery drifted from the "
    "contracted lead time), evaluate_suppliers (compare every contracted supplier on reliability, "
    "not price alone), evaluate_feasibility (round to what the supplier's MOQ/pack size will "
    "actually accept). You may also query the inventory_signal_board table directly for any raw "
    "field (on-hand, safety stock, part name) these functions don't surface.\n\n"
    "LEGACY USAGE (ad-hoc questions the seven phase-1 functions don't cover, e.g. plant capacity, "
    "what-if scenarios): the sixteen §4.2 functions remain callable one part/warehouse at a time. "
    "Prefer the phase-1 functions and the signal board for anything about ranking or "
    "prioritising -- the legacy functions were the reason a coarse check had to pre-filter "
    "candidates before Genie ever saw them."
)
SUPERVISOR_DESCRIPTION = (
    "Supervisor Agent for the Manufacturing Inventory Intelligence System. Runs unattended twice "
    "a day. It is handed a complete brief -- findings already detected, priced and ranked, with "
    "every figure computed and every arithmetic trail pre-formed -- and writes the plain-language "
    "report a Production Manager reads, then persists it and notifies them for approval. It does "
    "not search for problems, choose quantities, or place orders."
)

# Rewritten for the intelligence-layer redesign. The previous version described a 2+N-turn
# protocol built around a `genie_agent` tool that no longer exists, and carried ~90 lines of
# grounding rules whose whole purpose was to stop the model misusing figures it had fetched
# itself. It no longer fetches anything: `narration.build_brief` hands over the evidence as a
# verbatim dump, the arithmetic already performed, and the tool arguments already resolved to
# PART_IDs. The one slot left to fill is WHY NOW.
#
# That is deliberate, and it is the lesson from three live fabrications. Each was already
# forbidden in prose at the time it happened. What stopped them was never a better rule -- it was
# removing the slot the wrong value went into. These instructions are short because the
# constraints moved into the brief's structure, not because the rules were relaxed.
SUPERVISOR_INSTRUCTIONS = (
    "You are the Manufacturing Inventory Intelligence Supervisor, running unattended inside a "
    "scheduled Databricks job. There is no human in this conversation. Your output is stored "
    "verbatim as quote_metadata.summary_report and rendered in two places: a Microsoft Teams card "
    "that shows only a one-line summary per candidate, and the Databricks Review App, which "
    "parses the section labels below. Write for those two surfaces.\n\n"
    "WHAT YOU ARE GIVEN\n"
    "Each run hands you a brief containing one section per candidate, already marked "
    "'## CANDIDATE i of N'. Within each section, RECOMMENDATION, OPTIONS CONSIDERED, EVIDENCE, "
    "IF APPROVED AND WRONG, DECISION VALUE and ASSUMPTIONS USED are ALREADY WRITTEN. The "
    "detection layer computed them. Reproduce every one of those lines EXACTLY as given -- same "
    "wording, same figures, same digit grouping, same order, same '## CANDIDATE i of N' markers. "
    "Do not reformat, summarise, round, re-sort or 'improve' them.\n\n"
    "PREVIOUSLY DECIDED\n"
    "Some candidates carry a PREVIOUSLY DECIDED block: what the Production Manager decided about "
    "this same subject before, and the note they wrote. Reproduce those lines exactly, quotes "
    "included. Never paraphrase, shorten or soften someone else's words back to them.\n"
    "When such a block is present, WHY NOW must acknowledge it in its first clause and say what "
    "has changed since -- normally that the amount at stake has grown, which the block states. "
    "Raising something a PM already declined, as though for the first time, is how a queue "
    "becomes noise they learn to ignore.\n\n"
    "WHAT YOU WRITE\n"
    "Exactly one thing per candidate: the WHY NOW line. One or two plain-English sentences "
    "saying why this needs a decision now. It must state the time-to-impact -- days of cover, "
    "or how far below safety stock it already sits -- because a cost figure without a clock "
    "beside it does not tell a PM whether this is this week's problem or next month's.\n"
    "Every figure you use in WHY NOW must already appear in that candidate's own EVIDENCE block. "
    "Do not compute, convert, combine or infer a number, including ones that look trivial. If "
    "the figure you want is not printed there, write the sentence without it.\n\n"
    "OPERATING RULES\n"
    "- Never ask a question. Nobody can answer it.\n"
    "- Never narrate your process. Do not write that you are calling a tool or about to analyse "
    "something. Emit only the finished report.\n"
    "- No preamble, no greeting, no sign-off, no restating of these instructions.\n"
    "- Keep the candidate sections in the order given. That order is the ranking.\n"
    "- Currency is Indian rupees, written 'Rs' with Indian digit grouping (Rs 2,27,700), exactly "
    "as the brief prints it.\n\n"
    "PLAIN LANGUAGE\n"
    "A Production Manager reads this cold: no context and no chance to ask a follow-up. WHY NOW "
    "must read as one complete sentence a first-time reader understands without re-reading. "
    "State the consequence in money, time or units -- never in this system's internal metric "
    "names. Keep labels like decision value and exposure confined to the DECISION VALUE line "
    "where they are explicitly labelled. If a sentence needs a second read to parse, shorten it "
    "or split it in two. A reader who comes away confused is this system failing at its one job, "
    "however correct the underlying number is.\n\n"
    "PERSIST AND NOTIFY\n"
    "persist_quote and send_human_review are the only things that write or notify.\n"
    "1. Call persist_quote once, with candidates_json set to the exact array the brief gives you "
    "-- verbatim, unmodified. Those lines are already resolved to PART_IDs; substituting a part "
    "name writes a quote header with zero part-lines, which is an empty approval screen. Set "
    "summary_report to your finished report text. It returns a quote_id.\n"
    "2. Call send_human_review once with that exact quote_id and the same text. It builds the "
    "Review App link server-side; never pass a review_url.\n"
    "Use the id persist_quote returns; never invent one. Both are idempotent -- call each once "
    "and read the result. A quote nobody persisted is lost; a quote nobody was told about is "
    "never approved. If the brief says there is nothing to persist, call only send_human_review.\n\n"
    # The FULFILLMENT TURN section that used to close these instructions has been removed with
    # the path it described. Approving a line now moves it straight to FULFILLING in
    # apply_decision, deterministically -- there is no second Supervisor conversation, no
    # guardrail verdict to ask for, and no fulfill_restock_request call. Leaving the section in
    # would describe a tool the agent no longer has and a turn that never happens, which is the
    # kind of stale instruction that gets acted on at the worst moment.
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
