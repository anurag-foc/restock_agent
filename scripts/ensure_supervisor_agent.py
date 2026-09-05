"""Idempotently ensure the Inventory Intelligence Supervisor Agent + tools exist.

`create_supervisor_agent.py` is intentionally a one-shot "as code" record --
running it twice creates two Supervisor Agents. This script is the safe
wrapper meant to be called from an automated "deploy everything" flow
(see `scripts/deploy_all.sh`):

1. Looks up an existing agent by display name (`SUPERVISOR_DISPLAY_NAME`).
   - Found -> reuse it. Syncs its description/instructions to the current
     values in `create_supervisor_agent.py` if they've drifted, and reconciles
     its tool set to be *exactly* the two declared in `build_tool_specs`:
       * `fulfillment_guardrail` -- fulfillment re-check (Genie Space, read-only)
       * `inventory_intelligence_actions`    -- persist/notify/fulfill (the
                                    mcp-inventory-actions app, attached
                                    directly via the `app` tool type)
     Anything else is removed. That guard originally existed to stop UC
     functions being attached directly, which let the Supervisor bypass Genie
     for analysis. Since the intelligence-layer redesign it does more: it is
     what REMOVES `genie_agent` from an already-deployed agent, because the
     detectors now attach every figure to the finding and there is nothing
     left for the Supervisor to look up. Running this script is the migration
     step, not merely a check.
   - Not found -> create it fresh via the same config as
     `create_supervisor_agent.py`.
2. Writes the resulting endpoint name into the `supervisor_endpoint_name`
   job parameter default of every job in `JOB_YAMLS`, so no job points at a
   stale/deleted endpoint. No-ops if already correct.

The fulfillment guardrail's Genie Space id is auto-discovered from
`databricks bundle summary` for the given target (generated at deploy time,
so not known ahead of deploy), unless passed explicitly. The `--genie-space-*`
arguments are retained so existing callers and `deploy_all.sh` keep working,
but are ignored: that space is no longer attached.

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
    REPO_ROOT / "resources/jobs/lakeflow_trigger_job.yml",
    # restock_decision_job.yml is deliberately absent: since the fulfillment restructure it
    # applies decisions deterministically and never calls the Supervisor.
    # Any new job that calls the Supervisor must be listed here, or it will keep
    # pointing at a stale endpoint after the agent is re-created.
    REPO_ROOT / "resources/jobs/intelligence_job.yml",
]
# Not in the tool spec any more, so `ensure_tools` deletes it on the next run. Named here only
# so the removal is greppable: this is the deep-analysis Genie Space the redesign took off the
# critical path, not a tool that went missing by accident.
REMOVED_ANALYSIS_TOOL_ID = "genie_agent"

GUARDRAIL_TOOL_ID = "fulfillment_guardrail"
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


def build_tool_specs(guardrail_space_id: str) -> dict[str, Tool]:
    """The exact tool set the Supervisor is supposed to have.

    **One tool since the fulfillment restructure. It was three, then two.**
    ``genie_agent`` -- the deep-analysis Genie Space over 23 UC functions -- has been
    removed. It is no longer on the critical path: the detectors compute every figure and
    attach it to the finding as ``evidence_json``, so by the time the Supervisor is called
    there is nothing left to look up. Its single remaining turn writes the report from a
    complete brief and calls the action tools.

    That is not just a simplification, it closes the original failure mode. The reason Genie
    existed was to stop the Supervisor reasoning from whatever it could reach instead of doing
    real analysis. The redesign removes the need differently and more firmly: the brief IS the
    analysis, and the way to guarantee it gets used is for there to be nothing else to consult.

    ``fulfillment_guardrail`` has now gone too. It re-checked an approved line against live
    stock before acting, which is a purchase-shaped question: meaningful for PURCHASE and
    TRANSFER, meaningless for RECALIBRATE, REVIEW_STOCK and the three supplier-grain finding
    types. Approving a line now moves it straight to FULFILLING, and the one case the guardrail
    genuinely caught -- a purchase approved days later that an open PO already covers -- is a
    deterministic advisory inside ``apply_decision``, no LLM involved.

    - ``inventory_intelligence_actions`` -- the mcp-inventory-actions app, attached via the
      ``app`` tool type (app authorization), exposing persist_quote / send_human_review /
      fulfill_restock_request. The ONLY tool that writes or notifies. It exists because UC
      functions cannot: a SQL function body rejects DML outright. Each tool behind it enforces
      its own idempotency server-side rather than trusting the model to call it once.
      ``fulfill_restock_request`` is no longer called by any path but stays exposed -- removing
      a tool is a separate decision from removing its caller.

    **The residual risk that used to be recorded here is gone.** With the guardrail attached,
    "nothing to consult" was true of the analysis space but not literally true, since a
    narration turn could still reach four functions. Now there is genuinely nothing to consult:
    the brief is the only source of fact the model has, which is the strongest form of the
    guarantee this design has been reaching for since the first revision.
    """
    _ = guardrail_space_id  # accepted for call-site compatibility; no longer attached
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
    that: it is what actually removes ``genie_agent`` from a previously-deployed agent, since
    the redesign took the analysis space off the critical path. Running this script is therefore
    the migration step, not just a check.
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
    # Two shapes: a job-level `parameters:` entry (lakeflow_trigger_job.yml,
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
        "--guardrail-space-resource-key",
        default="fulfillment_guardrail",
        help="Resource key of the fulfillment Genie space (default: fulfillment_guardrail)",
    )
    parser.add_argument(
        "--guardrail-space-id",
        default=None,
        help="Skip auto-discovery and use this Fulfillment Guardrail Genie Space id directly",
    )
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    # The deep-analysis Genie Space is deliberately NOT discovered or attached any more -- see
    # build_tool_specs. The --genie-space-* arguments are kept so existing invocations and
    # deploy_all.sh do not break; they are ignored.
    guardrail_space_id = args.guardrail_space_id or discover_genie_space_id(
        args.target, args.profile, args.guardrail_space_resource_key
    )
    print(f"Genie space id ({args.guardrail_space_resource_key}): {guardrail_space_id}")
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
    ensure_tools(w, parent, build_tool_specs(guardrail_space_id))

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
