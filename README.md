# Restockify

Databricks multi-agent pipeline that watches inventory, predicts stockouts, and
routes a human-in-the-loop approval (Microsoft Teams → Databricks Review App)
before triggering fulfillment.

- Requirements source: [`docs/prd.md`](docs/prd.md) — informal requirements from the project stakeholder.
- Design source: [`docs/architecture.md`](docs/architecture.md) — the team's refined architecture (this is what implementation follows).
- **[`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md) — how the architecture maps onto real Databricks Agent Bricks primitives (Genie Space, Supervisor Agent, UC functions) and what's actually deployed. Read this before touching agent-related code.**
- [`docs/uc_functions_reference.md`](docs/uc_functions_reference.md) — function-level reference for every Unity Catalog function the Genie Agent calls: signature, what it computes, when to use it, edge cases, and examples.

## Project layout

```
databricks.yml              # Bundle root config (targets: dev, prod; engine: direct for genie_spaces)
resources/
  jobs/                      # Databricks Jobs (Lakeflow trigger, schema bootstrap, deploy_uc_functions)
  genie/                     # Genie Space DAB resource
  apps/                      # Databricks App resources (review app, once built)
notebooks/
  schema_bootstrap.ipynb     # Creates ab_training.agentic_restock + quote_metadata (our owned artifacts)
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

## Data layer

Two catalogs, two owners (see `src/agentic_restock/config.py` for the single
source of truth on catalog/schema/table names — override via
`AGENTIC_RESTOCK_GOLD_CATALOG` / `AGENTIC_RESTOCK_DIM_SCHEMA` /
`AGENTIC_RESTOCK_FACTS_SCHEMA` / `AGENTIC_RESTOCK_CATALOG` /
`AGENTIC_RESTOCK_SCHEMA` env vars):

**Data Engineering's `gold_dev` star schema** (real data, read-mostly):

| Table | Purpose |
|---|---|
| `gold_dev.supply_chain_analytics.fact_inventory_snapshot` | Daily stock snapshot per part/warehouse (`QUANTITY_ON_HAND`, `SAFETY_STOCK_QTY` = reorder trigger, `MAX_STOCK_LEVEL` = restock target, `STOCKOUT_RISK`) |
| `gold_dev.supply_chain_analytics.fact_inventory_transaction` | Stock movements (`RECEIPT`/`ISSUE`/`TRANSFER`); `ISSUE` rows are consumption events, feeding the Genie Agent's trend analysis |
| `gold_dev.supply_chain_analytics.fact_procurement` | Purchase orders — used by the restock veto and lead-time estimation |
| `gold_dev.supply_chain_analytics.fact_restock_request` | Quote + fulfillment lifecycle, one row per requested part-line per quote, joined to `gold_dev.dim.dim_request_status` for status/urgency/decision |
| `gold_dev.dim.dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`, ... | Dimension lookups (`PART_ID`/`WAREHOUSE_ID` business keys ↔ surrogate keys) |

**Our own `ab_training.agentic_restock` schema** (artifacts we own outright):

| Table | Purpose |
|---|---|
| `quote_metadata` | Teams/Review-App fields per quote (`summary_report`, `teams_message_id`, `databricks_preview_url`, `decision_comments`) that have no home in `fact_restock_request`, keyed by `quote_id` |

Plus the §4.2 Unity Catalog functions (see below).

Reset/seed `quote_metadata` by running the `schema_bootstrap` job (see below).

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
databricks bundle run schema_bootstrap -t dev     # (re)seed quote_metadata
```

**Or, to deploy everything in one shot** (bundle validate/deploy, UC functions,
and an idempotent create-or-reuse of the Supervisor Agent + its tools):

```bash
./scripts/deploy_all.sh dev              # add SEED=true to also reseed quote_metadata
```

See [`scripts/deploy_all.sh`](scripts/deploy_all.sh) and
[`scripts/ensure_supervisor_agent.py`](scripts/ensure_supervisor_agent.py) for what
each step does and why the Supervisor Agent step is safe to re-run.

Full runbook (targets, variables, adding new jobs/apps, troubleshooting): [`docs/USAGE.md`](docs/USAGE.md).

## Implementation roadmap

1. ~~Repo/bundle scaffold~~
2. ~~Lakeflow trigger job (§4.1 coarse low-stock check)~~ — `resources/jobs/lakeflow_trigger_job.yml`, hourly, ships `PAUSED`. Branches on candidate count via an if/else condition task.
3. ~~§4.2 deep-analysis logic as Unity Catalog SQL functions~~ — `notebooks/uc_functions/deep_analysis_functions.ipynb`, deployed via the `deploy_uc_functions` job.
4. ~~Genie Agent (Genie Space) over the gold_dev star schema, with the UC functions as trusted assets~~ — `resources/genie/genie_agent.genie_space.yml`.
5. ~~Supervisor Agent, wired to the Genie Space + UC functions as tools, invoked from the Lakeflow job~~ — see `scripts/create_supervisor_agent.py` and `notebooks/lakeflow_trigger/invoke_supervisor.py`. Verified working end-to-end against the live endpoint.
6. Supervisor Agent writes to `gold_dev.supply_chain_analytics.fact_restock_request` + `quote_metadata`
7. Teams Adaptive Card notification
8. Databricks Review App (live quote preview, Approve/Reject)
9. Restock Agent (real-time re-validation, `fact_restock_request` fulfillment write)
10. MLflow evaluation + monitoring
11. End-to-end deployment as a Databricks App

See [`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md) for the
full detail on steps 3–5 (why they look the way they do, what's Beta/SDK-only
vs. DAB-native, and what was verified).
