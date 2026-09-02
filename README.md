# Inventory Intelligence

A Databricks Agent Bricks pipeline that rebuilds a full inventory signal board
twice a day, has an LLM pick and analyse the **single** highest-decision-value
action on it, and routes that one recommendation through a human approval
(Microsoft Teams → Databricks Review App) before anything is ordered.

The "one action, twice a day" shape is the product thesis, not a limitation:
sending a person forty low-stock alerts a day reproduces exactly the alert
fatigue this is meant to fix.

## Start here

- **[`docs/end_to_end_walkthrough.md`](docs/end_to_end_walkthrough.md)** — the pipeline in plain language, with an architecture diagram. Read this first.
- **[`docs/market_evidence_phase1.md`](docs/market_evidence_phase1.md)** — the phase-1 requirements and product reasoning: the seven intelligence nuances, why twice daily, and the open validation items. Current source of truth for *what* the system should do.
- **[`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md)** — how it maps onto real Agent Bricks primitives (Genie Spaces, Supervisor Agent, MCP action app, UC functions) and why each piece looks the way it does. **Read before touching anything agent-related.**
- [`docs/uc_functions_reference.md`](docs/uc_functions_reference.md) — per-function reference for all 23 Unity Catalog functions the Genie Space calls.
- [`docs/USAGE.md`](docs/USAGE.md) — deployment runbook (targets, variables, adding jobs/apps, troubleshooting).

Historical record, superseded — useful for "why was it ever like that", not for
current behaviour: [`docs/prd.md`](docs/prd.md) and
[`docs/architecture.md`](docs/architecture.md) (the original single-layer
design), [`prd_v2.md`](prd_v2.md) (the 4-tier requirements behind the 16
deep-analysis functions), and `docs/agentic_coarse_check_design.md`.

## How it runs

```
lakeflow_trigger job (07:00 + 15:00 UTC, ships PAUSED)
  refresh_signal_board → rebuilds inventory_signal_board: one row per
                         (part, warehouse), every nuance as a column
  invoke_supervisor    → counts ranked actions; exits NO_ACTION at zero.
                         Otherwise 3 turns with the Supervisor Agent:
                         rank → analyse → persist + notify

  → Teams Adaptive Card → restock-review app

restock_decision job (triggered by the app's Final Submit)
  apply_decision     → deterministic per-line status write, no LLM
  has_approval       → condition task, approved_count > 0
  invoke_fulfillment → one fresh Supervisor turn per approved line:
                       fulfillment_guardrail → fulfill_restock_request
```

**The invariant that governs everything:** analysis is always reached through a
natural-language interface (a Genie Space over UC SQL functions); writes are
always reached through an idempotent action tool (a custom MCP app). The
Supervisor Agent holds exactly three tools and never calls a UC function
directly.

## Project layout

```
databricks.yml               # Bundle root (targets: dev, prod; engine: direct for genie_spaces)
resources/
  jobs/                      # lakeflow_trigger, restock_decision, schema_bootstrap,
                             #   deploy_uc_functions, generate_sim_data
  genie/                     # genie_agent + fulfillment_guardrail Genie Space resources
  apps/                      # restock-review + mcp-inventory-actions app resources
notebooks/
  signal_board/              # refresh_signal_board.py — rebuilds the board
  lakeflow_trigger/          # invoke_supervisor.py — the 3-turn conversation
  restock_decision/          # apply_decision.py, invoke_fulfillment.py
  uc_functions/              # deep_analysis_functions.ipynb (16), priority_functions.py (7)
  genie/                     # Serialized Genie Space configs (*.geniespace.json)
  simulation/                # generate_sim_data.py — simulated history + ground truth
  schema_bootstrap.ipynb     # Creates quote_metadata
src/agentic_restock/
  config.py                  # Single source of truth: catalog/schema + table names
  jobs/signal_board.py       # The board query (pure string builder, no Spark import)
  jobs/priority_functions.py # The 7 phase-1 UC function definitions
  jobs/run_log.py            # scan_run_log — records quiet runs too
mcp-inventory-actions/       # Python MCP app: the ONLY thing that writes
restock-review/              # AppKit (Node/React) review app
scripts/                     # deploy_all.sh + Supervisor Agent lifecycle + dev scripts
tests/                       # pytest unit tests
```

## Data layer

Everything lives in `gold_dev`. See `src/agentic_restock/config.py` for the
single source of truth on names — override via
`AGENTIC_RESTOCK_GOLD_CATALOG` / `_DIM_SCHEMA` / `_FACTS_SCHEMA` / `_CATALOG` /
`_SCHEMA`. The ownership split is still real even though the location no longer
differs:

**Data Engineering's, read-only:**

| Table | Purpose |
|---|---|
| `supply_chain_analytics.fact_inventory_snapshot` | *Daily* stock snapshot per part/warehouse (`QUANTITY_ON_HAND`, `SAFETY_STOCK_QTY` = reorder trigger, `MAX_STOCK_LEVEL` = restock target). Every read must take the latest `SNAPSHOT_DATE_KEY` per pair. |
| `supply_chain_analytics.fact_inventory_transaction` | Stock movements; `ISSUE` rows are consumption, feeding burn rate and seasonality |
| `supply_chain_analytics.fact_procurement` | Purchase orders — open-commitment suppression and lead-time estimation |
| `dim.dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`, `dim_request_status` | Dimension lookups (`PART_ID` business keys ↔ `PART_KEY` surrogate keys; joins need `IS_CURRENT = true`) |

**DE's, but we write to it:**

| Table | Purpose |
|---|---|
| `supply_chain_analytics.fact_restock_request` | Quote + fulfillment lifecycle, **one row per part-line**. Appended by `persist_quote`, updated by `apply_decision` / `fulfill_restock_request` / the app's `/complete` endpoint. Its `NOTE` column was added by `scripts/add_restock_note_column.py`. |

**Ours:**

| Table | Purpose |
|---|---|
| `quote_metadata` | Per-quote Teams/Review-App fields (`summary_report`, `teams_message_id`, `databricks_preview_url`) that have no home in a per-part-line table. Created by `schema_bootstrap`. |
| `inventory_signal_board` | Rebuilt wholesale each run; the Genie Space's primary read surface |
| `scan_run_log` | One row per scan run, **including quiet ones** — so "nothing needed attention" is distinguishable from "the job broke" |
| `dim_bom`, `dim_supplier_contract`, `fact_plant_capacity` | BOM cascade, contracted lead times, capacity |
| `sim_events`, `sim_pair_scenarios` | Ground truth planted by `generate_sim_data`, so a detection claim can be attributed rather than asserted |

Plus the 23 Unity Catalog functions.

Real inventory data is DE-managed; `schema_bootstrap` only touches
`quote_metadata`. `generate_sim_data` is the exception — insert-only against
DE's facts, but it *does* delete and replace fact rows inside its generated
date window.

## Local dev

```bash
uv sync              # installs deps into .venv, including dev tools
uv run pytest -q     # unit tests (no Databricks runtime needed)
uv run ruff check src scripts tests
```

## Deploying

This repo is a [Databricks Asset Bundle](https://docs.databricks.com/en/dev-tools/bundles/index.html) —
the only supported way it gets pushed to Databricks. No manual notebook uploads
or click-ops job creation, except the Supervisor Agent, which has no DAB
resource type yet.

```bash
databricks auth login --profile anurag-r   # re-auth if the token has expired
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run schema_bootstrap -t dev     # (re)seed quote_metadata
```

**Or, in one shot** (validate → deploy → UC functions → idempotent
create-or-reuse of the Supervisor Agent and its tools → redeploy):

```bash
./scripts/deploy_all.sh dev              # SEED=true also reseeds quote_metadata
```

Always pass `--profile <name>`; never let the CLI pick a default.

**One manual step after a first deploy:** the `mcp-inventory-actions` app's
auto-provisioned service principal needs Unity Catalog grants on the tables it
writes. `deploy_all.sh` prints the resolved service principal id as a reminder
but does not apply them — the exact `GRANT` statements are in
[`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md) §2.7. A missing
grant shows up as a *silent SQL failure inside a tool call*, not an auth error.

## Status

Deployed and verified end to end: signal board, 23 UC functions, both Genie
Spaces, the Supervisor Agent and its three tools, the MCP action app, the Teams
card, the review app (per-line approve/reject, batched submit, fulfilling
orders), and the fulfillment path.

Not built: MLflow evaluation and monitoring.

The `lakeflow_trigger` job still ships **`PAUSED`**. The path works; what's
unresolved is whether the `decision_value` ranking actually reorders anything
versus raw exposure on real data, and whether its cost weights are right —
both open items in [`docs/market_evidence_phase1.md`](docs/market_evidence_phase1.md)
§16. Unpausing starts writing real quotes and paging real people on a schedule,
so it waits on those.
