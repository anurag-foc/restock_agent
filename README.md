# Inventory Intelligence

A Databricks Agent Bricks pipeline that scans manufacturing inventory twice a
day, detects up to eight distinct kinds of problem (stockout risk, BOM cascade
blocks, lateral-transfer opportunities, dead capital, lead-time drift, demand
shift, supplier economics, MOQ-uneconomic orders), prices each one in real
rupees, picks the highest-value handful under a hard output budget, and routes
them through a human approval (Microsoft Teams → Databricks Review App)
before anything is ordered.

The "a few actions, twice a day, bundled into one notification" shape is the
product thesis, not a limitation: sending a person forty low-stock alerts a
day reproduces exactly the alert fatigue this is meant to fix. One real MRP
run produced 8,366 action messages in a week — scarcity of output is the
feature.

## Start here

- **[`docs/end_to_end_walkthrough.md`](docs/end_to_end_walkthrough.md)** — the pipeline in plain language, with an architecture diagram: what happens from the Lakeflow job's first task through to a PM marking a delivery received. Read this first.
- **[`docs/redesign_tracker.md`](docs/redesign_tracker.md)** — the living record of how this pipeline was built: every phase, every gate, every bug that produced plausible-but-wrong output rather than failing. Read this for *why* things are the way they are.
- **[`docs/intelligence_layer_design.md`](docs/intelligence_layer_design.md)** — the design: detection at each nuance's natural grain, exposure as `P(stockout) × consequence`, real rupee costs from real fix construction.
- **[`docs/dataset_generator_spec.md`](docs/dataset_generator_spec.md)** — the dataset the detectors are measured against.
- **[`docs/schema_changes_gold_dev_analytics.md`](docs/schema_changes_gold_dev_analytics.md)** — where the replica catalog (`gold_dev_analytics`) diverges from Data Engineering's real schema, and why.
- **[`docs/agent_bricks_mapping.md`](docs/agent_bricks_mapping.md)** — how this maps onto real Agent Bricks primitives (Supervisor Agent, MCP action app). **Predates the redesign and needs a re-read against `docs/redesign_tracker.md` before trusting its specifics** — the Supervisor is now a one-tool agent, and both Genie Spaces it describes are gone.

Historical record, superseded — useful for "why was it ever like that", not
for current behaviour: [`docs/prd.md`](docs/prd.md), [`docs/architecture.md`](docs/architecture.md)
(the original single-layer design), [`prd_v2.md`](prd_v2.md) (the 4-tier
requirements behind the 16 deep-analysis functions),
[`docs/market_evidence_phase1.md`](docs/market_evidence_phase1.md) and
[`docs/uc_functions_reference.md`](docs/uc_functions_reference.md) (the
phase-1 signal-board design this redesign replaced), and
`docs/agentic_coarse_check_design.md`.

## How it runs

```
Lakeflow Job "intelligence" (07:00 + 15:00 UTC, ships PAUSED)
  refresh_positions   → builds part_position / parent_cascade /
                        supplier_performance from 3 years of history: a
                        corrected-measures layer (burn rate, lead-time
                        distribution, cascade rollup), no ranking in it
  run_intelligence    → 8 scanners find every problem at its own natural
                        grain → suppress findings with a live decision
                        against them → collapse duplicates → apply a hard
                        output budget (default 4) with a diversity
                        constraint → build one brief with every figure
                        pre-computed → ONE Supervisor turn: write the WHY NOW
                        prose, call persist_quote then send_human_review

  → Teams Adaptive Card → restock-review app

restock_decision job (triggered by the app's Final Submit)
  apply_decision      → deterministic per-line status write, no LLM.
                        APPROVED goes straight to FULFILLING at the proposed
                        quantity, plus an advisory check (an open PO may
                        already cover it) -- not blocking, since the PM has
                        just said to do it.

  → PM marks a line delivered on the Fulfilling Orders page
    → POST /api/lines/:lineKey/complete writes COMPLETED directly
```

**The invariant that governs everything:** analysis is deterministic Python
(estimators + scanners), computed and attached to each finding as evidence
*before* the Supervisor ever sees it; writes are always reached through an
idempotent action tool (a custom MCP app). The Supervisor Agent holds exactly
**one** tool and never calls a UC function or a Genie Space directly — there
is nothing left for it to look up, which is what makes the guarantee hold.

## Project layout

```
databricks.yml               # Bundle root (targets: dev, prod)
resources/
  jobs/                      # intelligence, restock_decision, schema_bootstrap,
                             #   deploy_uc_functions
  apps/                      # restock-review + mcp-inventory-actions app resources
notebooks/
  positions/                 # refresh_positions.py — the corrected-measures layer
  lakeflow_trigger/          # run_intelligence.py — the single Supervisor turn
  restock_decision/          # apply_decision.py
  uc_functions/              # deep_analysis_functions.ipynb (16 legacy functions;
                             #   only pending_procurement_qty is still called,
                             #   directly by SQL, not through Genie)
  schema_bootstrap.ipynb     # Creates quote_metadata
src/agentic_restock/
  config.py                  # Single source of truth: catalog/schema + table names
  estimators/                # burn.py (E1), leadtime.py (E2), cascade.py, risk.py
  detectors/                 # findings.py, fixes.py (FX1-FX3), scanners.py (S1-S8),
                             #   selection.py (suppression, diversity, budget)
  generation/                # the dataset generator (policy, entities, bom, demand,
                             #   contracts, snapshots, ground_truth, ...)
  jobs/positions.py          # part_position / parent_cascade / supplier_performance
  jobs/run_intelligence.py   # the single-turn Supervisor invocation helper
  jobs/run_log.py            # scan_run_log — records quiet runs too
  narration.py                # builds the brief: evidence dump, arithmetic
                              #   pre-formed, tool arguments pre-resolved
  money.py                    # Indian-scale rupee formatting
mcp-inventory-actions/       # Python MCP app: the ONLY thing that writes
                             #   (persist_quote, send_human_review)
restock-review/              # AppKit (Node/React) review app
scripts/                     # deploy_all.sh + Supervisor Agent lifecycle + dataset generator
tests/                       # pytest unit tests
```

## Data layer

`AGENTIC_RESTOCK_GOLD_CATALOG` currently points this pipeline at
`gold_dev_analytics` — a replica catalog this project owns, kept alongside
Data Engineering's real `gold_dev` so the redesign could be measured without
touching production data. See `src/agentic_restock/config.py` for the single
source of truth on names.

**Data Engineering's, read-only in shape** — `fact_inventory_snapshot`,
`fact_inventory_transaction` (`ISSUE` rows = consumption),
`fact_procurement`, `fact_supplier_delivery`, `fact_supplier_quality`, plus
`dim.dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`,
`dim_request_status`, `dim_vehicle_model`.

**DE's, but we write to it** — `fact_restock_request`: one row per
part-line. Appended by `persist_quote`, updated by `apply_decision` / the
app's `/complete` endpoint. Carries `ACTION_TYPE`, `FINDING_TYPE`,
`SUBJECT_KEY`, `SOURCE_WAREHOUSE_KEY`, `RECOMMENDED_SUPPLIER_KEY`,
`EXPOSURE_AT_DECISION`, `DECISION_REASON` — added for the redesign so all
eight finding types (not just purchases) are decidable rows.

**Ours** — `quote_metadata` (per-quote Teams/Review-App fields), `part_position`
/ `parent_cascade` / `supplier_performance` (the corrected-measures layer,
rebuilt each run by `refresh_positions`), `scan_run_log` (one row per run,
including quiet ones), `dim_bom` / `dim_model_bom` / `dim_supplier_contract`.

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
create-or-reuse of the Supervisor Agent and its tool → redeploy):

```bash
./scripts/deploy_all.sh dev              # SEED=true also reseeds quote_metadata
```

Always pass `--profile <name>`; never let the CLI pick a default.

**One manual step after a first deploy:** the `mcp-inventory-actions` app's
auto-provisioned service principal needs Unity Catalog grants on the tables it
writes. `deploy_all.sh` prints the resolved service principal id as a reminder
but does not apply them — a missing grant shows up as a *silent SQL failure
inside a tool call*, not an auth error.

## Status

The redesign is complete: dataset, estimators, risk/consequence, detectors,
selection, and delivery all built and gated (see `docs/redesign_tracker.md`).
The phase-1 pipeline it replaced — the signal board, the 8 phase-1 UC
functions, both Genie Spaces, the 2+N-turn Supervisor protocol, and the
Supervisor-mediated fulfillment turn — has been removed rather than kept
side by side; there is one pipeline now.

Not built: MLflow evaluation and monitoring, a production catalog cutover
(`gold_dev_analytics` → `gold_dev`).

The `intelligence` job still ships **`PAUSED`**. The path works end to end
against the replica; what remains is operational — grant the MCP app's
service principal access on `gold_dev_analytics`, run it against real
production data, and unpause.
