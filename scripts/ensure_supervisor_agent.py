"""Idempotently ensure the Restockify Supervisor Agent + tools exist.

`create_supervisor_agent.py` is intentionally a one-shot "as code" record --
running it twice creates two Supervisor Agents. This script is the safe
wrapper meant to be called from an automated "deploy everything" flow
(see `scripts/deploy_all.sh`):

1. Looks up an existing agent by display name (`SUPERVISOR_DISPLAY_NAME`).
   - Found -> reuse it. Syncs its description/instructions to the current
     values in `create_supervisor_agent.py` if they've drifted, and reconciles
     its tool set to be *exactly* `{genie_agent}` -- adds it if missing,
     removes anything else (e.g. the §4.2 UC functions attached directly by
     an older revision of this script, which let the Supervisor bypass Genie
     entirely for analysis).
   - Not found -> create it fresh via the same config as
     `create_supervisor_agent.py`.
2. Writes the resulting endpoint name into the `supervisor_endpoint_name`
   job parameter default in `resources/jobs/lakeflow_trigger_job.yml`, so the
   Lakeflow job never points at a stale/deleted endpoint. No-ops if it's
   already correct.

The Genie Space id is auto-discovered from `databricks bundle summary` for
the given target (it's generated at deploy time and isn't known ahead of
deploy), unless passed explicitly with --genie-space-id.

Usage:
    python scripts/ensure_supervisor_agent.py --profile anurag-r --target dev
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.service.supervisoragents import GenieSpace, SupervisorAgent, Tool

from create_supervisor_agent import (
    GENIE_TOOL_DESCRIPTION,
    SUPERVISOR_DESCRIPTION,
    SUPERVISOR_DISPLAY_NAME,
    SUPERVISOR_INSTRUCTIONS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JOB_YAML = REPO_ROOT / "resources/jobs/lakeflow_trigger_job.yml"
GENIE_TOOL_ID = "genie_agent"


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


def ensure_tools(w: WorkspaceClient, parent: str, genie_space_id: str) -> None:
    """Reconcile the agent's tool set to be exactly {genie_agent}.

    The Supervisor deliberately has no direct access to the §4.2 UC functions
    (avg_daily_consumption, classify_urgency, pending_procurement_qty, etc.) --
    those are trusted assets on the Genie Space only. Any other tool found here
    is leftover from an older revision that attached them directly (observed
    letting the Supervisor call them and skip the Genie Agent entirely), and
    is removed.
    """
    existing_tools = {t.tool_id: t for t in w.supervisor_agents.list_tools(parent=parent)}

    if GENIE_TOOL_ID not in existing_tools:
        w.supervisor_agents.create_tool(
            parent=parent,
            tool_id=GENIE_TOOL_ID,
            tool=Tool(
                tool_type="genie_space",
                description=GENIE_TOOL_DESCRIPTION,
                genie_space=GenieSpace(id=genie_space_id, space_id=genie_space_id),
            ),
        )
        print(f"  + added missing tool: {GENIE_TOOL_ID}")
    else:
        genie_tool = existing_tools[GENIE_TOOL_ID]
        if genie_tool.description != GENIE_TOOL_DESCRIPTION:
            w.supervisor_agents.update_tool(
                name=genie_tool.name,
                tool=Tool(tool_type="genie_space", description=GENIE_TOOL_DESCRIPTION),
                update_mask=FieldMask(field_mask=["description"]),
            )
            print(f"  ~ updated stale description on tool: {GENIE_TOOL_ID}")
        else:
            print(f"  = tool already present: {GENIE_TOOL_ID}")

    extra_tool_ids = set(existing_tools) - {GENIE_TOOL_ID}
    for tool_id in sorted(extra_tool_ids):
        w.supervisor_agents.delete_tool(name=existing_tools[tool_id].name)
        print(f"  - removed extra tool (Supervisor should only have {GENIE_TOOL_ID}): {tool_id}")


def sync_job_yaml(endpoint_name: str) -> bool:
    text = JOB_YAML.read_text()
    pattern = re.compile(r"(name: supervisor_endpoint_name\s*\n\s*default: )(\S+)")
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"Could not find supervisor_endpoint_name parameter in {JOB_YAML}")

    if match.group(2) == endpoint_name:
        return False

    JOB_YAML.write_text(pattern.sub(rf"\g<1>{endpoint_name}", text, count=1))
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
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    genie_space_id = args.genie_space_id or discover_genie_space_id(
        args.target, args.profile, args.genie_space_resource_key
    )
    print(f"Genie space id: {genie_space_id}")

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
    ensure_tools(w, parent, genie_space_id)

    changed = sync_job_yaml(endpoint_name)
    if changed:
        print(f"Updated {JOB_YAML.relative_to(REPO_ROOT)} -> supervisor_endpoint_name default = {endpoint_name}")
        print("Re-run `databricks bundle deploy` to push this to the workspace.")
    else:
        print(f"{JOB_YAML.relative_to(REPO_ROOT)} already points at {endpoint_name} -- no change.")

    print(f"\nSupervisor Agent ready. Endpoint: {endpoint_name}")


if __name__ == "__main__":
    sys.exit(main())
