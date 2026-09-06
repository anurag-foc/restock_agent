"""Idempotently ensure the Inventory Intelligence Supervisor Agent + tools exist.

`create_supervisor_agent.py` is intentionally a one-shot "as code" record --
running it twice creates two Supervisor Agents. This script is the safe
wrapper meant to be called from an automated "deploy everything" flow
(see `scripts/deploy_all.sh`):

1. Looks up an existing agent by display name (`SUPERVISOR_DISPLAY_NAME`).
   - Found -> reuse it. Syncs its description/instructions to the current
     values in `create_supervisor_agent.py` if they've drifted, and reconciles
     its tool set to be *exactly* the one declared in `build_tool_specs`:
       * `inventory_intelligence_actions` -- persist/notify (the
         mcp-inventory-actions app, attached directly via the `app` tool type)
     Anything else is removed. That guard originally existed to stop UC
     functions being attached directly, which let the Supervisor bypass Genie
     for analysis. The intelligence-layer redesign made it do more: it is what
     removes `genie_agent` and `fulfillment_guardrail` from an
     already-deployed agent, since the detectors now attach every figure to
     the finding and the fulfillment turn was replaced by a deterministic
     check inside `apply_decision` -- there is nothing left for either Genie
     Space to do. Running this script is the migration step, not merely a
     check.
   - Not found -> create it fresh via the same config as
     `create_supervisor_agent.py`.
2. Writes the resulting endpoint name into the `supervisor_endpoint_name`
   job parameter default of every job in `JOB_YAMLS`, so no job points at a
   stale/deleted endpoint. No-ops if already correct.

Both Genie Spaces this agent used to call (`genie_agent`, the phase-1
priority-function analysis space, and `fulfillment_guardrail`, the
fulfillment re-check) have been retired along with the phase-1 pipeline --
see docs/redesign_tracker.md's Retirement section. There is no Genie Space
discovery here any more; the Supervisor's only tool is the MCP actions app.

Prerequisite: the `mcp-inventory-actions` app must already be deployed (part
of `databricks bundle deploy`) before running this, and its service principal
needs Unity Catalog grants on fact_restock_request/quote_metadata/etc. -- see
docs/agent_bricks_mapping.md.

Usage:
    python scripts/ensure_supervisor_agent.py --profile anurag-r --target dev
"""

import argparse
import re
import sys
from pathlib import Path

from create_supervisor_agent import (
    ACTIONS_TOOL_DESCRIPTION,
    SUPERVISOR_DESCRIPTION,
    SUPERVISOR_DISPLAY_NAME,
    SUPERVISOR_INSTRUCTIONS,
)
from databricks.sdk import WorkspaceClient
from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.service.supervisoragents import (
    App,
    SupervisorAgent,
    Tool,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JOB_YAMLS = [
    # restock_decision_job.yml is deliberately absent: since the fulfillment restructure it
    # applies decisions deterministically and never calls the Supervisor.
    # Any new job that calls the Supervisor must be listed here, or it will keep
    # pointing at a stale endpoint after the agent is re-created.
    REPO_ROOT / "resources/jobs/intelligence_job.yml",
]

ACTIONS_TOOL_ID = "inventory_intelligence_actions"

# Custom MCP server (mcp-inventory-actions app) attached directly via the
# `app` tool type -- app authorization, not a UC HTTP Connection. See
# resources/apps/mcp_inventory_actions_app.yml and docs/agent_bricks_mapping.md.
ACTIONS_APP_NAME = "mcp-inventory-actions"


def find_existing_agent(w: WorkspaceClient):
    for agent in w.supervisor_agents.list_supervisor_agents():
        if agent.display_name == SUPERVISOR_DISPLAY_NAME:
            return agent
    return None


def sync_agent_text(w: WorkspaceClient, agent) -> None:
    """Push the current description/instructions onto an existing agent if they've drifted."""
    stale_fields = []
    if agent.description != SUPERVISOR_DESCRIPTION:
        stale_fields.append("description")
    if agent.instructions != SUPERVISOR_INSTRUCTIONS:
        stale_fields.append("instructions")

    if not stale_fields:
        print("  = description/instructions already up to date")
        return

    w.supervisor_agents.update_supervisor_agent(
        name=agent.name,
        supervisor_agent=SupervisorAgent(
            display_name=SUPERVISOR_DISPLAY_NAME,
            description=SUPERVISOR_DESCRIPTION,
            instructions=SUPERVISOR_INSTRUCTIONS,
        ),
        update_mask=FieldMask(field_mask=stale_fields),
    )
    print(f"  ~ updated stale field(s): {', '.join(stale_fields)}")


def build_tool_specs() -> dict[str, Tool]:
    """The exact tool set the Supervisor is supposed to have.

    **One tool.** It was three, then two, now one. `genie_agent` -- the deep-analysis Genie
    Space over the old §4.2 functions -- went with the detector redesign: the scanners compute
    every figure and attach it to the finding as `evidence_json`, so by the time the Supervisor is
    called there is nothing left to look up. `fulfillment_guardrail` went with the fulfillment
    restructure: an approval now moves a line straight to FULFILLING, and the one case the
    guardrail genuinely caught -- a purchase approved days later that an open PO already covers --
    is a deterministic advisory inside `apply_decision`, no LLM involved.

    That is not just a simplification, it closes the original failure mode. Genie existed to stop
    the Supervisor reasoning from whatever it could reach instead of doing real analysis. The
    redesign removes the need differently and more firmly: the brief IS the analysis, and the way
    to guarantee it gets used is for there to be nothing else to consult.

    - ``inventory_intelligence_actions`` -- the mcp-inventory-actions app, attached via the
      ``app`` tool type (app authorization), exposing persist_quote / send_human_review. The
      ONLY tool that writes or notifies. It exists because UC functions cannot: a SQL function
      body rejects DML outright. Each tool behind it enforces its own idempotency server-side
      rather than trusting the model to call it once.
    """
    return {
        ACTIONS_TOOL_ID: Tool(
            tool_type="app",
            description=ACTIONS_TOOL_DESCRIPTION,
            app=App(name=ACTIONS_APP_NAME),
        ),
    }


def ensure_tools(w: WorkspaceClient, parent: str, tool_specs: dict[str, Tool]) -> None:
    """Reconcile the agent's tool set to be exactly ``tool_specs``.

    Anything not in the spec is deleted. Historically this guard existed to stop UC functions
    being re-attached directly, which let the Supervisor bypass Genie. It now does more than
    that: it is what actually removes `genie_agent` / `fulfillment_guardrail` from a
    previously-deployed agent. Running this script is therefore the migration step, not just a
    check.
    """
    existing_tools = {t.tool_id: t for t in w.supervisor_agents.list_tools(parent=parent)}

    for tool_id, spec in tool_specs.items():
        existing = existing_tools.get(tool_id)
        if existing is None:
            w.supervisor_agents.create_tool(parent=parent, tool_id=tool_id, tool=spec)
            print(f"  + added missing tool: {tool_id} ({spec.tool_type})")
        elif existing.description != spec.description:
            w.supervisor_agents.update_tool(
                name=existing.name,
                tool=Tool(tool_type=spec.tool_type, description=spec.description),
                update_mask=FieldMask(field_mask=["description"]),
            )
            print(f"  ~ updated stale description on tool: {tool_id}")
        else:
            print(f"  = tool already present: {tool_id}")

    extra_tool_ids = set(existing_tools) - set(tool_specs)
    for tool_id in sorted(extra_tool_ids):
        w.supervisor_agents.delete_tool(name=existing_tools[tool_id].name)
        print(f"  - removed unexpected tool (not in the declared tool set): {tool_id}")


def sync_job_yaml(job_yaml: Path, endpoint_name: str) -> bool:
    text = job_yaml.read_text()
    # Two shapes: a job-level `parameters:` entry (intelligence_job.yml,
    # triggered by schedule -- job_parameters work fine there), or a literal
    # default directly on a notebook task's base_parameters
    # (restock_decision_job.yml -- must NOT be a job-level parameter, since
    # the restock-review app triggers it via AppKit's jobs() plugin, which
    # for taskType="notebook" always sends legacy notebook_params; the Jobs
    # API rejects notebook_params on a job that also has job-level
    # `parameters:` configured). The negative lookahead skips the templated
    # "{{job.parameters.supervisor_endpoint_name}}" base_parameter value.
    patterns = [
        re.compile(r"(name: supervisor_endpoint_name\s*\n\s*default: )(\S+)"),
        re.compile(r"(supervisor_endpoint_name: )(?!\"\{\{)(\S+)"),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        if match.group(2) == endpoint_name:
            return False
        job_yaml.write_text(pattern.sub(rf"\g<1>{endpoint_name}", text, count=1))
        return True

    raise SystemExit(f"Could not find supervisor_endpoint_name parameter in {job_yaml}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="~/.databrickscfg profile to use")
    parser.add_argument("--target", default="dev", help="Bundle target (dev/prod), default dev")
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    print(f"Actions MCP app: {ACTIONS_APP_NAME}")

    agent = find_existing_agent(w)
    if agent is not None:
        print(f"Found existing supervisor agent: {agent.name} (endpoint: {agent.endpoint_name})")
        parent = agent.name
        endpoint_name = agent.endpoint_name
        print("Syncing description/instructions:")
        sync_agent_text(w, agent)
    else:
        print(f"No existing '{SUPERVISOR_DISPLAY_NAME}' found -- creating one.")
        created = w.supervisor_agents.create_supervisor_agent(
            supervisor_agent=SupervisorAgent(
                display_name=SUPERVISOR_DISPLAY_NAME,
                description=SUPERVISOR_DESCRIPTION,
                instructions=SUPERVISOR_INSTRUCTIONS,
            )
        )
        print(f"Created supervisor agent: {created.name} (endpoint: {created.endpoint_name})")
        parent = created.name
        endpoint_name = created.endpoint_name

    print("Ensuring tools:")
    ensure_tools(w, parent, build_tool_specs())

    any_changed = False
    for job_yaml in JOB_YAMLS:
        changed = sync_job_yaml(job_yaml, endpoint_name)
        any_changed = any_changed or changed
        if changed:
            print(f"Updated {job_yaml.relative_to(REPO_ROOT)} -> supervisor_endpoint_name default = {endpoint_name}")
        else:
            print(f"{job_yaml.relative_to(REPO_ROOT)} already points at {endpoint_name} -- no change.")
    if any_changed:
        print("Re-run `databricks bundle deploy` to push this to the workspace.")

    print(f"\nSupervisor Agent ready. Endpoint: {endpoint_name}")


if __name__ == "__main__":
    sys.exit(main())
