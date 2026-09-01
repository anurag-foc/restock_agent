"""Idempotently ensure the Inventory Intelligence Supervisor Agent + tools exist.

`create_supervisor_agent.py` is intentionally a one-shot "as code" record --
running it twice creates two Supervisor Agents. This script is the safe
wrapper meant to be called from an automated "deploy everything" flow
(see `scripts/deploy_all.sh`):

1. Looks up an existing agent by display name (`SUPERVISOR_DISPLAY_NAME`).
   - Found -> reuse it. Syncs its description/instructions to the current
     values in `create_supervisor_agent.py` if they've drifted, and reconciles
     its tool set to be *exactly* the three declared in `build_tool_specs`:
       * `genie_agent`           -- deep analysis (Genie Space, read-only)
       * `restock_request_maker` -- fulfillment re-check (Genie Space, read-only)
       * `inventory_intelligence_actions`    -- persist/notify/fulfill (the
                                    mcp-inventory-actions app, attached
                                    directly via the `app` tool type)
     Anything else is removed. That guard originally existed to stop the §4.2
     UC functions being attached directly, which let the Supervisor bypass
     Genie entirely for analysis; it still enforces that, against a three-tool
     set rather than a one-tool set.
   - Not found -> create it fresh via the same config as
     `create_supervisor_agent.py`.
2. Writes the resulting endpoint name into the `supervisor_endpoint_name`
   job parameter default of every job in `JOB_YAMLS`, so no job points at a
   stale/deleted endpoint. No-ops if already correct.

Both Genie Space ids are auto-discovered from `databricks bundle summary` for
the given target (they're generated at deploy time and aren't known ahead of
deploy), unless passed explicitly.

Prerequisite: the `mcp-inventory-actions` app must already be deployed (part
of `databricks bundle deploy`) before running this, and its service principal
needs Unity Catalog grants on fact_restock_request/quote_metadata/etc. -- see
docs/agent_bricks_mapping.md.

Usage:
    python scripts/ensure_supervisor_agent.py --profile anurag-r --target dev
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.service.supervisoragents import App, GenieSpace, SupervisorAgent, Tool

from create_supervisor_agent import (
    ACTIONS_TOOL_DESCRIPTION,
    GENIE_TOOL_DESCRIPTION,
    RESTOCK_MAKER_TOOL_DESCRIPTION,
    SUPERVISOR_DESCRIPTION,
    SUPERVISOR_DISPLAY_NAME,
    SUPERVISOR_INSTRUCTIONS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JOB_YAMLS = [
    REPO_ROOT / "resources/jobs/lakeflow_trigger_job.yml",
    REPO_ROOT / "resources/jobs/restock_decision_job.yml",
]
GENIE_TOOL_ID = "genie_agent"
RESTOCK_MAKER_TOOL_ID = "restock_request_maker"
ACTIONS_TOOL_ID = "inventory_intelligence_actions"

# Custom MCP server (mcp-inventory-actions app) attached directly via the
# `app` tool type -- app authorization, not a UC HTTP Connection. See
# resources/apps/mcp_inventory_actions_app.yml and docs/agent_bricks_mapping.md.
ACTIONS_APP_NAME = os.environ.get("INVENTORY_ACTIONS_APP", "mcp-inventory-actions")


def discover_genie_space_id(target: str, profile: str | None, resource_key: str) -> str:
    cmd = ["databricks", "bundle", "summary", "-t", target, "-o", "json"]
    if profile:
        cmd += ["--profile", profile]
    summary = json.loads(subprocess.check_output(cmd, cwd=REPO_ROOT, text=True))
    try:
        return summary["resources"]["genie_spaces"][resource_key]["id"]
    except KeyError as e:
        raise SystemExit(
            f"Could not find genie space '{resource_key}' in `databricks bundle summary -t {target}` "
            f"output -- has the bundle been deployed yet? ({e})"
        )


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


def build_tool_specs(genie_space_id: str, restock_maker_space_id: str) -> dict[str, Tool]:
    """The exact tool set the Supervisor is supposed to have.

    Two analysis tools and one action tool:

    - ``genie_agent`` -- the §4.2 deep-analysis Genie Space. The Supervisor
      still has NO direct access to the underlying UC functions; an earlier
      revision attached them directly and the Supervisor promptly called them
      straight from candidate JSON, skipping Genie entirely. Analysis stays
      behind a natural-language interface.
    - ``restock_request_maker`` -- a second, narrower Genie Space used only at
      fulfillment time to re-check an already-approved line against live stock.
      Also read-only.
    - ``inventory_intelligence_actions`` -- the mcp-inventory-actions app, attached
      directly via the `app` tool type (app authorization), exposing
      persist_quote / send_human_review / fulfill_restock_request. This is
      the ONLY tool that writes or notifies. It exists because UC functions
      cannot: a SQL function body rejects DML outright. Every tool behind it
      enforces its own idempotency server-side rather than trusting the model
      to call it exactly once.
    """
    return {
        GENIE_TOOL_ID: Tool(
            tool_type="genie_space",
            description=GENIE_TOOL_DESCRIPTION,
            genie_space=GenieSpace(id=genie_space_id, space_id=genie_space_id),
        ),
        RESTOCK_MAKER_TOOL_ID: Tool(
            tool_type="genie_space",
            description=RESTOCK_MAKER_TOOL_DESCRIPTION,
            genie_space=GenieSpace(id=restock_maker_space_id, space_id=restock_maker_space_id),
        ),
        ACTIONS_TOOL_ID: Tool(
            tool_type="app",
            description=ACTIONS_TOOL_DESCRIPTION,
            app=App(name=ACTIONS_APP_NAME),
        ),
    }


def ensure_tools(w: WorkspaceClient, parent: str, tool_specs: dict[str, Tool]) -> None:
    """Reconcile the agent's tool set to be exactly ``tool_specs``.

    Anything not in the spec is deleted -- historically this guard existed to
    stop UC functions being re-attached directly (which let the Supervisor
    bypass Genie). It still enforces that, it just enforces a three-tool set
    now instead of a one-tool set.
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
    pattern = re.compile(r"(name: supervisor_endpoint_name\s*\n\s*default: )(\S+)")
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"Could not find supervisor_endpoint_name parameter in {job_yaml}")

    if match.group(2) == endpoint_name:
        return False

    job_yaml.write_text(pattern.sub(rf"\g<1>{endpoint_name}", text, count=1))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="~/.databrickscfg profile to use")
    parser.add_argument("--target", default="dev", help="Bundle target (dev/prod), default dev")
    parser.add_argument(
        "--genie-space-resource-key",
        default="genie_agent",
        help="Resource key of the genie_spaces entry in resources/genie/*.yml (default: genie_agent)",
    )
    parser.add_argument(
        "--genie-space-id",
        default=None,
        help="Skip auto-discovery and use this Genie Space id directly",
    )
    parser.add_argument(
        "--restock-maker-space-resource-key",
        default="restock_request_maker",
        help="Resource key of the fulfillment Genie space (default: restock_request_maker)",
    )
    parser.add_argument(
        "--restock-maker-space-id",
        default=None,
        help="Skip auto-discovery and use this Restock Request Maker Genie Space id directly",
    )
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    genie_space_id = args.genie_space_id or discover_genie_space_id(
        args.target, args.profile, args.genie_space_resource_key
    )
    print(f"Genie space id ({args.genie_space_resource_key}): {genie_space_id}")

    restock_maker_space_id = args.restock_maker_space_id or discover_genie_space_id(
        args.target, args.profile, args.restock_maker_space_resource_key
    )
    print(f"Genie space id ({args.restock_maker_space_resource_key}): {restock_maker_space_id}")
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
    ensure_tools(w, parent, build_tool_specs(genie_space_id, restock_maker_space_id))

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
