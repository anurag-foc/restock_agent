# Agentic Restock

Databricks multi-agent pipeline that watches inventory, predicts stockouts, and
routes a human-in-the-loop approval (Microsoft Teams → Databricks Review App)
before triggering fulfillment.

- Requirements source: [`docs/prd.md`](docs/prd.md) — informal requirements from the project stakeholder.
- Design source: [`docs/architecture.md`](docs/architecture.md) — the team's refined architecture (this is what implementation follows).

## Project layout

```
databricks.yml              # Bundle root config (targets: dev, prod)
resources/
  jobs/                      # Databricks Jobs (Lakeflow trigger, schema bootstrap, ...)
  apps/                      # Databricks App resources (review app, once built)
notebooks/
  schema_bootstrap.ipynb     # Mocks the 5-table schema until Data Engineering delivers real tables
  lakeflow_trigger/          # coarse_check.py + invoke_supervisor_stub.py (run by the job below)
src/agentic_restock/
  config.py                  # Single source of truth: catalog/schema + table name constants
  jobs/                      # Lakeflow trigger job logic (architecture §4.1)
  agents/                    # Supervisor / Genie / Restock agent implementations
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
2. ~~Lakeflow trigger job (§4.1 coarse low-stock check)~~ — `resources/jobs/lakeflow_trigger_job.yml`, hourly, ships `PAUSED`. Branches on candidate count via an if/else condition task; `invoke_supervisor_stub` is a placeholder until step 4 below replaces it with a real Supervisor Agent call.
3. Genie Agent (deep analysis, stockout forecast, urgency, quote)
4. Supervisor Agent (orchestration, `open_request` writes, HITL handoff)
5. Teams Adaptive Card notification
6. Databricks Review App (live quote preview, Approve/Reject)
7. Restock Agent (real-time re-validation, `restock_requests` write)
8. MLflow evaluation + monitoring
9. End-to-end deployment as a Databricks App
