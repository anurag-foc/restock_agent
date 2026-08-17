# Agentic Restock

Databricks multi-agent pipeline that watches inventory, predicts stockouts, and
routes a human-in-the-loop approval (Microsoft Teams → Databricks Review App)
before triggering fulfillment.

- Requirements source: [`docs/prd.md`](docs/prd.md) — informal requirements from the project stakeholder.
- Design source: [`docs/architecture.md`](docs/architecture.md) — the team's refined architecture (this is what implementation follows).
- **[`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md) — how the architecture maps onto real Databricks Agent Bricks primitives (Genie Space, Supervisor Agent, UC functions) and what's actually deployed. Read this before touching agent-related code.**

## Project layout

```
databricks.yml              # Bundle root config (targets: dev, prod; engine: direct for genie_spaces)
resources/
  jobs/                      # Databricks Jobs (Lakeflow trigger, schema bootstrap, deploy_uc_functions)
  genie/                     # Genie Space DAB resource
  apps/                      # Databricks App resources (review app, once built)
notebooks/
  schema_bootstrap.ipynb     # Mocks the 5-table schema until Data Engineering delivers real tables
  lakeflow_trigger/          # coarse_check.py + invoke_supervisor.py (run by the job below)
  genie/                     # Serialized Genie Space config (genie_agent.geniespace.json)
  uc_functions/              # §4.2 deep-analysis logic as UC SQL functions (deep_analysis_functions.ipynb)
scripts/
  create_supervisor_agent.py # "As code" record for creating the Supervisor Agent + tools (SDK-only, no DAB resource type yet)
src/agentic_restock/
  config.py                  # Single source of truth: catalog/schema + table name constants
  jobs/                      # Lakeflow trigger job logic (architecture §4.1)
  agents/                    # Pointers to the Agent Bricks resources (Genie Space, Supervisor Agent) — not Python classes
  integrations/              # Teams Adaptive Card notifications
tests/                       # pytest unit tests
```

## Data layer (mock, until Data Engineering delivers real tables)

All 5 tables live in `ab_training.agentic_restock` (see `src/agentic_restock/config.py`
for the single source of truth on catalog/schema names — override via
`AGENTIC_RESTOCK_CATALOG` / `AGENTIC_RESTOCK_SCHEMA` env vars when real tables land):

| Table | Purpose |
|---|---|
| `inventory_stock_level` | Real-time stock snapshot per item/warehouse |
| `threshold_config_table` | Reorder/target thresholds per item/warehouse |
| `consumption_history` | Daily consumption, feeds Genie Agent's trend analysis |
| `open_request` | Quote + approval lifecycle (`PENDING_APPROVAL` → `APPROVED`/`REJECTED` → `FULFILLING` → `COMPLETED`/`NEEDS_REVIEW`) |
| `restock_requests` | Fulfillment ledger, written only after approval + real-time re-validation |

Reset/seed the mock data by running the `schema_bootstrap` job (see below).

## Local dev setup

```bash
uv sync              # installs deps into .venv, including dev tools
uv run pytest -q     # run unit tests
```

## Deploying to Databricks

This repo is a [Databricks Asset Bundle](https://docs.databricks.com/en/dev-tools/bundles/index.html) —
that's the only supported way this project gets pushed to Databricks (no manual
notebook uploads or click-ops job creation).

```bash
databricks auth login --profile anurag-r   # re-auth if the token has expired
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run schema_bootstrap -t dev     # (re)seed the mock schema
```

Full runbook (targets, variables, adding new jobs/apps, troubleshooting): [`docs/USAGE.md`](docs/USAGE.md).

## Implementation roadmap

1. ~~Repo/bundle scaffold~~
2. ~~Lakeflow trigger job (§4.1 coarse low-stock check)~~ — `resources/jobs/lakeflow_trigger_job.yml`, hourly, ships `PAUSED`. Branches on candidate count via an if/else condition task.
3. ~~§4.2 deep-analysis logic as Unity Catalog SQL functions~~ — `notebooks/uc_functions/deep_analysis_functions.ipynb`, deployed via the `deploy_uc_functions` job.
4. ~~Genie Agent (Genie Space) over the 3 source tables, with the UC functions as trusted assets~~ — `resources/genie/genie_agent.genie_space.yml`.
5. ~~Supervisor Agent, wired to the Genie Space + UC functions as tools, invoked from the Lakeflow job~~ — see `scripts/create_supervisor_agent.py` and `notebooks/lakeflow_trigger/invoke_supervisor.py`. Verified working end-to-end against the live endpoint.
6. `open_request` table + Supervisor Agent writes
7. Teams Adaptive Card notification
8. Databricks Review App (live quote preview, Approve/Reject)
9. Restock Agent (real-time re-validation, `restock_requests` write)
10. MLflow evaluation + monitoring
11. End-to-end deployment as a Databricks App

See [`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md) for the
full detail on steps 3–5 (why they look the way they do, what's Beta/SDK-only
vs. DAB-native, and what was verified).
