# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Inventory Intelligence" / "Manufacturing Inventory Intelligence Engine" — a Databricks **Agent Bricks** pipeline that rebuilds a full inventory signal board twice a day, has an LLM pick and analyse the *single* highest-decision-value action on it, persists a restock quote, and notifies a human in Microsoft Teams for approval.

The "one action, twice a day" shape is deliberate and is the product thesis, not a limitation: `docs/market_evidence_phase1.md` §3 is the argument that an hourly cadence emitting every flagged part reproduces the alert fatigue this product exists to fix. Read that doc plus [docs/end_to_end_walkthrough.md](docs/end_to_end_walkthrough.md) (the plain-language walk-through of the current pipeline) before proposing a design change.

Deployment is **exclusively** via Databricks Asset Bundles (`databricks.yml` + `resources/**/*.yml`). No manual notebook uploads, no click-ops job creation — except the Supervisor Agent, which has no DAB resource type yet (see below).

## Commands

```bash
uv sync                                  # install deps into .venv (editable install of src/)
uv run pytest -q                         # unit tests (no Databricks runtime needed)
uv run pytest tests/test_config.py::test_qualified_table_uses_own_catalog_and_schema  # single test
uv run ruff check src scripts tests   # NOT `ruff check .` -- notebooks/ is not
                                     # ruff-clean by design (implicit spark/dbutils,
                                     # % magic comments) and drowns real findings

databricks auth login --profile anurag-r # re-auth when the refresh token expires
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run <job_key> -t dev   # job_key = resources.jobs.<key>, e.g. schema_bootstrap
databricks bundle run generate_sim_data -t dev  # ~18mo of simulated facts + sim_events ground truth
databricks bundle summary -t dev         # also the only way to read the deployed Genie Space id

./scripts/deploy_all.sh dev              # validate → deploy → UC functions → ensure agent → redeploy
                                         # SEED=true also reruns schema_bootstrap (wipes quote_metadata)
```

Always pass `--profile <name>`; never let the CLI pick a default. Targets: `dev` (default) and `prod`, both deploying to `/Workspace/Shared/.bundle/agentic_restock/<target>`.

Scripts that hit the live workspace are run with `PYTHONPATH=src python3 scripts/<x>.py` (they are not pytest tests): `run_e2e_pipeline.py` (the full pipeline outside the job — mirrors `invoke_supervisor.py`'s three turns; `--dry-run` stops before the Supervisor call, `--skip-board-refresh` reuses the existing board), `test_teams_card.py` (card rendering; dry-run without `TEAMS_WEBHOOK_URL`), `validate_genie_groundedness.py` (3 golden scenarios checking Genie reasons rather than echoing snapshot flags), `add_restock_note_column.py` (one-off idempotent `ALTER TABLE` adding `fact_restock_request.NOTE`, which holds the PM's free-text approve/reject reasoning).

## Architecture — the invariant that governs everything

**Analysis is always reached through a natural-language interface; writes are always reached through an idempotent action tool.** Deterministic analysis logic lives in **Unity Catalog SQL functions**, which are trusted assets of **Genie Spaces** — the Supervisor never calls those functions directly. Nothing is a Python "agent class."

```
Lakeflow Job (07:00 + 15:00 UTC, PAUSED)
  refresh_signal_board → CREATE OR REPLACE inventory_signal_board: one row per
                         (part, warehouse), every phase-1 nuance as a column
  invoke_supervisor    → COUNT(*) over rank_priority_actions(5); exits NO_ACTION at 0.
                         Otherwise a 3-turn conversation with the Supervisor endpoint;
                         the agent itself calls persist_quote then send_human_review

restock-review app (Databricks App)
  PM stages approve/reject + a note per line, then one Final Submit
    → triggers the restock_decision job with a batched decisions_json
    apply_decision     → deterministic status write for every line (no LLM)
    has_approval       → condition task, approved_count > 0
    invoke_fulfillment → one fresh Supervisor turn per approved line:
                         fulfillment_guardrail → fulfill_restock_request
  Later, on the Fulfilling Orders page, the PM marks a line delivered
    → POST /api/lines/:lineKey/complete writes COMPLETED directly (see below)

Supervisor Agent (SDK-only, Beta) — exactly three tools:
  genie_agent           genie_space → 23 UC SQL functions (deep analysis, read-only)
  fulfillment_guardrail genie_space → fulfillment re-check (read-only)
  inventory_intelligence_actions  app → custom MCP server, mcp-inventory-actions app
                                        (persist_quote, send_human_review,
                                         fulfill_restock_request)
```

**The Supervisor's tool set is exactly those three**, reconciled by `scripts/ensure_supervisor_agent.py`, which deletes anything else it finds. The rule this enforces: **never attach UC functions to the Supervisor directly.** An earlier revision did, and the Supervisor then called them straight from candidate JSON and never invoked Genie at all, defeating the design. Analysis stays behind Genie; only `inventory_intelligence_actions` writes.

**Why the action tools are a custom MCP app and not `uc_function`** (verified, do not re-litigate by trying again):
- A UC **SQL** function body containing `INSERT` fails with `PARSE_SYNTAX_ERROR` — DML is not permitted.
- Hence: a custom MCP server (`mcp-inventory-actions/`, built from Databricks' official "MCP Server - Hello World" app template), attached to the Supervisor directly via the `app` tool type. `app` tool type only accepts an `mcp-`- or `agent-`-prefixed app name, and reaches it via **app authorization** rather than a UC HTTP Connection — no service principal/secret scope to set up for the Supervisor's side. The app's own auto-provisioned service principal still needs Unity Catalog grants on the tables it writes (see `docs/agent_bricks_mapping.md`) — a one-time `GRANT`, not an OAuth/secret-scope dance.

**Every action tool enforces its own idempotency server-side** (`mcp-inventory-actions/server/tools.py`) — the Supervisor is an LLM and may retry or double-call, and a duplicate `fact_restock_request` row is a duplicate procurement order. `persist_quote` derives a deterministic `quote_id` from the candidate set + date; `send_human_review` no-ops if `teams_message_id` is set; `fulfill_restock_request` only acts on a line currently `APPROVED`. The notebooks *verify* the tools ran and fail loudly if not — they never retry them.

Read [docs/agent_bricks_mapping.md](docs/agent_bricks_mapping.md) before touching anything agent-related — it is the authoritative record of why each piece looks the way it does.

### Signal board (`src/agentic_restock/jobs/signal_board.py`)

Replaced the old coarse-check scanner (`jobs/lakeflow_trigger.py`, deleted). Pure string-building function (no Spark import) so it is unit-testable locally; `notebooks/signal_board/refresh_signal_board.py` is the thin notebook wrapper.

Where the coarse check emitted candidate *rows* filtered by one threshold, this emits a **table** — `CREATE OR REPLACE TABLE inventory_signal_board`, one row per (part, warehouse) over the full working set, with every phase-1 nuance as a **column** computed set-wise (window functions and joins, never a per-row scalar UDF call — a scalar function that scans a fact table in its body does not survive past a few hundred part/warehouse pairs).

The board is Genie's read surface, and the seven phase-1 UC functions are thin reads *over* it rather than independent computations, so there is exactly one source of truth about a part/warehouse. Column groups: stock position · seasonality-adjusted burn + `days_of_cover` · `contracted_lead_days` vs `observed_avg_delay_days` → `effective_lead_days` · `otd_rate`/`reliability_score` · `network_surplus_qty`/`best_donor_warehouse_id`/`donor_cover_after_days` · `threatened_parent_part_id`/`value_at_risk` · open-commitment state and age.

`fact_inventory_snapshot` is a *daily* fact, so every read takes the latest `SNAPSHOT_DATE_KEY` per `(PART_KEY, WAREHOUSE_KEY)` via `ROW_NUMBER()`. There is no `reorder_point`/`is_active` config table — `SAFETY_STOCK_QTY` is the reorder trigger and `MAX_STOCK_LEVEL` the restock target, both columns on the snapshot fact.

Two documented approximations, flagged in the module docstring and `docs/market_evidence_phase1.md` §16 — don't present either as settled: `value_at_risk` uses `MAX_STOCK_LEVEL - QUANTITY_ON_HAND` as a build-target proxy because there is no forecast/production-plan table, and the `decision_value` cost weights are a first-pass formula not yet validated against outcomes.

**Ordering constraint (a fresh workspace deadlocks if you get this wrong).** Spark validates a SQL function body at `CREATE` time, so the seven phase-1 functions can only be created *after* the board exists — hence `deploy_uc_functions` runs `refresh_signal_board` before `deploy_priority_functions`. This is why `refresh_signal_board.py` deliberately does *not* call `rank_priority_actions` to report a candidate count, useful as that would be in the job UI: on a fresh workspace the board refresh would fail on the missing function, so the function that would have fixed it never deploys. `invoke_supervisor.py` does the counting instead, since it only runs once the functions are known to exist.

### Priority functions (`src/agentic_restock/jobs/priority_functions.py`)

The seven phase-1 UC functions, all reading the board: `scan_transfer_options`, `scan_assembly_risk`, `rank_priority_actions`, `scan_demand_shift`, `scan_leadtime_drift`, `evaluate_suppliers`, `evaluate_feasibility`. Deployed by `deploy_uc_functions`' third task.

`rank_priority_actions` is the one that matters: `decision_value = GREATEST(exposure - action_cost, 0)`, where `action_cost` scales with how expensive the cheapest viable fix is (≈3% of exposure if a transfer covers it, 15–50% scaled by lead time if only a buy exists, 100% if nothing helps). The formula is intentionally simple and visible **so it can be argued with** — do not hide it behind a black box. Whether decision-value ranking actually differs from raw exposure ordering on real data is still an open validation item (§16).

**Suppression (nuance 8) lives inside `rank_priority_actions` as a `WHERE` clause**, not in Genie's instructions — an LLM that remembers to filter 97% of the time re-raises a rejected item about monthly. An open commitment suppresses the row only while it is *fresh*: `PENDING_APPROVAL`/`NEEDS_REVIEW` re-surface after 2 days, `APPROVED`/`FULFILLING` after `effective_lead_days + 3`, tagged `STALLED_COMMITMENT`, because their exposure keeps accruing while they sit. `REJECTED` stays permanently suppressed. Known simplification: re-surfacing is time-based only; "exposure grew 1.5x since the decision" needs an `EXPOSURE_AT_DECISION` captured at decision time, which `fact_restock_request` does not store.

Several of the older 16 deep-analysis functions are now redundant with board columns (`classify_urgency`, `predicted_stockout_date`, `dynamic_reorder_point`, `consumption_anomaly_score`, …), and the three narrative-STRING ones (`assembly_risk_report`, `financial_tradeoff_summary`, `plant_capacity_check`) were a workaround for having no LLM in the loop — Genie writes the sentence now. **They are deliberately not retired yet**: `fulfillment_guardrail`'s Genie Space and the fulfillment path were never audited for dependence on them. Audit before deleting.

### Scan run log (`src/agentic_restock/jobs/run_log.py`)

One `scan_run_log` row per scan run, written whether or not a Supervisor conversation opened (`NO_ACTION` vs `SUPERVISOR_INVOKED`). Without it, "nothing needed attention" and "the job silently broke" are indistinguishable from outside, and the alert-fatigue counterpoint the product leans on ("quiet on 8 of 14 runs this week") has no evidence behind it. Keep writing it on the quiet path.

### Review App (`restock-review/`)

An AppKit (Node/React) Databricks App, deployed as part of the same bundle (`resources/apps/restock_review_app.yml`, `source_code_path: ../../restock-review`). Four SQL queries under `config/queries/`: `pending_quotes`, `quote_header`, `quote_lines`, `fulfilling_lines`. Pages: `PendingQuotesPage`, `QuoteDetailPage`, `FulfillingOrdersPage`, plus `IntelligenceReport.tsx`, which parses the Supervisor's `summary_report` text into the structured OUTPUT CONTRACT sections the detail page renders.

A PM decides **per part-line** — `fact_restock_request`'s grain is one row per part-line, and `REQUEST_STATUS_KEY`/`DECISION_DATE_KEY`/`CONFIRMED_QTY`/`NOTE` are all per-line — but decisions are **staged in the UI and submitted as one batch**: `POST /api/quotes/:quoteId/decisions` takes `{lineKey, decision, note}[]`, pre-validates, and triggers the `restock_decision` job once with a `decisions_json` array. It does not write.

**One endpoint does write, deliberately:** `POST /api/lines/:lineKey/complete` flips a line `FULFILLING → COMPLETED` (and appends to `NOTE`) straight from the app. There is no LLM step and no guardrail in marking a delivery received, so routing it through a job would buy nothing but latency. It is idempotent — it only acts on a line currently `FULFILLING`, and appends to `NOTE` rather than overwriting so the approval-stage note survives. Everything the *agent* does still goes through the MCP action tools; this is a human's deterministic status flip, and it is the only write in the app.

Analytics caching is explicitly **disabled** (`cache: { enabled: false }` in `server/server.ts`). The default shared cache served stale rows after a decision was written, which on an approval screen means showing a PM that a line they just approved is still pending.

Local dev: `npm run dev` (port 8000). `useAnalyticsQuery` has no `refetch()`, so the UI forces a refresh by remounting via a changing `key`. Analytics query params must be wrapped (`sql.string(...)`) — the wire format is `{"__sql_type":"STRING","value":"..."}`, and a bare string is rejected server-side.

### Action MCP server (`mcp-inventory-actions/`)

A Python Databricks App built from Databricks' official "MCP Server - Hello World" template (FastMCP + FastAPI), deployed via `resources/apps/mcp_inventory_actions_app.yml`. Exposes `persist_quote`, `send_human_review`, `fulfill_restock_request` as MCP tools (`server/tools.py`), each idempotent by construction (see above). Runs SQL via the app's own service-principal-authenticated `WorkspaceClient` (`server/db.py`, `server/utils.py::get_workspace_client`) against `DATABRICKS_WAREHOUSE_ID` — not on-behalf-of-user auth, since the caller is the Supervisor Agent, not an interactive user. The app's service principal needs `USE CATALOG`/`USE SCHEMA`/`SELECT` on `gold_dev.dim` and `gold_dev.supply_chain_analytics`, plus `INSERT, UPDATE` on `fact_restock_request` and `quote_metadata`, plus `CAN_USE` on the SQL warehouse — grant these once after first deploy; a missing grant surfaces as a silent SQL failure inside a tool call, not an auth error, since app authorization to the Supervisor succeeds regardless.

### Supervisor invocation (`notebooks/lakeflow_trigger/invoke_supervisor.py`)

Several hard-won details live here; changing them tends to break things silently:

- The endpoint speaks the OpenAI **Responses API** shape (`{"input": [{"role", "content"}]}`), **not** Chat Completions. `serving_endpoints.query()` builds the wrong shape, so this calls `w.api_client.do("POST", "/serving-endpoints/{name}/invocations", ...)` directly. The reply text is buried in `response["output"][…]["content"][…]["output_text"]`.
- Timeouts must be passed as `WorkspaceClient(config=Config(http_timeout_seconds=..., retry_timeout_seconds=...))`. `WorkspaceClient()` rejects them as kwargs, and setting `w.config.*` after construction is read too late and silently has no effect — the symptom is `TimeoutError: Timed out after 0:05:00`.
- Model Serving has a **290s HTTP gateway ceiling** (per-turn timeout is set to 280s). Hence the fixed three-turn protocol, each request resetting the clock: **Turn 1** call `rank_priority_actions` and pick the top action, nothing else · **Turn 2** drill down with one or two of the six scan/evaluate functions and write the OUTPUT CONTRACT artifact · **Turn 3** call `persist_quote` then `send_human_review`. Turns 1 and 2 were combined in one round-trip first and reliably timed out — ranking plus drill-downs plus the write-up exceeds the ceiling. Do not merge them back.
- **Custom MCP tool calls require an explicit approval round-trip.** Any `app`-tool-type call comes back as an `mcp_approval_request` item instead of executing, and there is no way to disable that at registration or per-request time (both checked, neither honored). The endpoint is **stateless**, so `previous_response_id` chaining does *not* work — it returns `"Invalid message sequence. The approval response was in an unexpected position."` Continuing means resending the whole transcript: everything sent so far, plus every item from the prior response's `output` **verbatim**, plus an `mcp_approval_response`. `_invoke()` answers it inline within the same turn, bounded by `MAX_APPROVAL_ROUNDS = 5`. The same helper is duplicated in `invoke_fulfillment.py` and `scripts/run_e2e_pipeline.py`; fix all three together.
- **`invoke_supervisor.py` never reads the ranked rows** — only `COUNT(*)`. That is structural, not stylistic: an earlier revision handed the Supervisor pre-chewed candidate JSON and it reasoned straight from that, never calling Genie. The only way to keep Genie on the critical path is to genuinely not know the answer here. A `COUNT(*)` is a boolean fact ("is there work"), not a judgment.
- **`persist_quote`'s signature pre-dates the redesign** — it still expects `item_id, warehouse_id, current_stock_qty, reorder_point_qty, suggested_reorder_qty, initial_urgency`, not `rank_priority_actions`' output shape. Rather than a shim, Turn 3 tells the Supervisor to assemble `candidates_json` from the board itself. Updating the tool's contract to accept the action shape natively is a real open follow-up.

### UC SQL functions

**23 functions total in `gold_dev.supply_chain_analytics`, all attached to the Genie Space as trusted assets**, in two generations:
- **16 deep-analysis functions** — `notebooks/uc_functions/deep_analysis_functions.ipynb`, the Tier 1–4 intelligence set from `prd_v2.md`. Reference: [docs/uc_functions_reference.md](docs/uc_functions_reference.md).
- **7 phase-1 priority functions** — `notebooks/uc_functions/priority_functions.py` over `src/.../priority_functions.py` (see above).

All three tasks live in the on-demand `deploy_uc_functions` job (`deploy_functions` → `refresh_signal_board` → `deploy_priority_functions`; `CREATE OR REPLACE`, idempotent, order matters — see the deadlock note above). Plain SQL, not Python UDFs, so they stay queryable from a SQL editor for debugging. Two Spark constraints bite constantly:

- Internal calls between functions must be **fully qualified** (`gold_dev.supply_chain_analytics.avg_daily_consumption(...)`) or `CREATE FUNCTION` fails to resolve them.
- A **scalar** function whose body scans a table and filters to one row must wrap the result in `MAX(...)`. Spark's validator rejects correlated scalar subqueries it cannot prove return exactly one row, even when filtering on a composite primary key. Table-valued (`RETURNS TABLE`) functions are exempt and can use `WHERE`/`QUALIFY ROW_NUMBER()`.

There is deliberately **no single `needs_restock` boolean**. The restock veto is the reasoning this system exists to do: `rank_priority_actions` computes it as exposure against the cost of the cheapest viable fix, and Genie explains the result. Resist collapsing multi-step judgment into one opaque function — an earlier `needs_restock(part, warehouse) → BOOLEAN` let the agent only echo TRUE/FALSE, never distinguishing "fully covered" from "partially covered" from "unconfirmable".

## Data layer

`src/agentic_restock/config.py` is the single source of truth for catalog/schema/table names, overridable by `AGENTIC_RESTOCK_{GOLD_CATALOG,DIM_SCHEMA,FACTS_SCHEMA,CATALOG,SCHEMA}`. Import from it rather than hardcoding names.

Everything lives in `gold_dev` now — the old `ab_training.agentic_restock` schema is gone. `CATALOG`/`SCHEMA` (our artifacts) and `GOLD_CATALOG`/`FACTS_SCHEMA` (DE's facts) both default to `gold_dev.supply_chain_analytics`; they stay separate config knobs because the *ownership* distinction is still real even though the location no longer differs.

**Data Engineering's, read-only** — `fact_inventory_snapshot`, `fact_inventory_transaction` (`ISSUE` rows = consumption), `fact_procurement`, plus `gold_dev.dim`'s `dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`, `dim_request_status`. Business keys (`PART_ID`) ↔ surrogate keys (`PART_KEY`); dimension joins need `IS_CURRENT = true`.

**DE's, but we write to it** — `fact_restock_request`: one row per part-line, appended by `persist_quote` and updated by `fulfill_restock_request` / `apply_decision` / the app's `/complete` endpoint. Its `NOTE` column was added out-of-band by `scripts/add_restock_note_column.py`, since DE owns the DDL and it is not tracked here.

**Ours** —
- `quote_metadata` — per-quote Teams/Review-App fields (`summary_report`, `teams_message_id`, `databricks_preview_url`, `decision_comments`) that have no home in a per-part-line table. Created by `schema_bootstrap`.
- `inventory_signal_board` — rebuilt wholesale by `refresh_signal_board`; Genie's read surface.
- `scan_run_log` — one row per scan run, created on demand by `run_log.py`'s `CREATE TABLE IF NOT EXISTS`.
- `dim_bom`, `dim_supplier_contract`, `fact_plant_capacity` — named in `config.py` and read by the board query (BOM cascade, contracted lead times, capacity).
- `sim_events`, `sim_pair_scenarios` — ground truth planted by `generate_sim_data`, recording every stockout / late supplier / dead-stock pair / surplus it generated, so a detection claim can be attributed rather than asserted.

Real inventory data is DE-managed and never seeded by `schema_bootstrap`, which only touches `quote_metadata`. The `generate_sim_data` job is the exception and is **insert-only by design** — explicit column lists, column verification before writing, no `CREATE OR REPLACE`/`ALTER` against a DE table, dimensions read-only — but it *does* delete and replace existing fact rows inside its generated date window, and `dry_run` currently defaults to `"false"`. Re-running with the same `seed` reproduces the dataset byte for byte.

## DAB conventions

- `bundle: engine: direct` is required — the `genie_spaces` resource type does not work with the default engine.
- Do not hand-prefix resource `name`s. Each target declares `presets.name_prefix` (`"[dev] "` / `"[prod] "`) and the CLI prepends it.
- The `dev` target deliberately omits `mode: development`: dev mode forces `[dev <username>]` into every resource name and tag and rejects any `name_prefix` without that username. Its useful behaviors are declared explicitly instead (`pause_status: PAUSED` on the Lakeflow schedule).
- Notebook paths in a resource YAML resolve relative to **that YAML file**, not the bundle root — hence `../../notebooks/...`.
- `resources/*.yml` and `resources/**/*.yml` are globbed; a new file is picked up without editing `databricks.yml`.
- Never hardcode `supervisor_endpoint_name` in a job YAML. Endpoint names change on agent re-creation; `scripts/ensure_supervisor_agent.py` rewrites that default in place across every file in its `JOB_YAMLS` list (currently `lakeflow_trigger_job.yml` and `restock_decision_job.yml`) and `deploy_all.sh` redeploys afterwards. Add new jobs that call the Supervisor to that list.
- `presets.name_prefix` is **not** applied to `apps` resources — Databricks Apps names must be lowercase kebab-case, so the CLI skips the `[dev] `/`[prod] ` prefix there. Both targets point at the same workspace host, so a `prod` deploy would collide with `dev` on the app names `restock-review` and `mcp-inventory-actions`.
- `mcp-inventory-actions` must keep its `mcp-` prefix — the Supervisor's `app` tool type only accepts `mcp-`- or `agent-`-prefixed apps. `bundle deploy` deploys it like any other app; no separate connection-creation step is needed (unlike the old UC HTTP Connection approach). After first deploy, grant its service principal Unity Catalog access (see above) — `deploy_all.sh` prints a reminder with the resolved service principal id but does not apply the grant itself.
- `scripts/create_supervisor_agent.py` is the "as code" record of the agent's description/instructions/tool prompt. `ensure_supervisor_agent.py` imports those constants and is the idempotent reconciler — run *that* one in automation.

## Known drift in the working tree

Worth confirming before assuming a doc is current:

- `create_supervisor_agent.py` creates its Genie tool as `inventory_intelligence_engine`; `ensure_supervisor_agent.py` expects `genie_agent` and deletes everything else — so running the former then the latter deletes and recreates the tool under a different id. Run `ensure_supervisor_agent.py` in automation; treat `create_supervisor_agent.py` as the as-code record of the description/instructions only.
- **No test coverage on the phase-1 code.** `signal_board.py`, `priority_functions.py` and `run_log.py` are pure string-builders — locally testable by design, exactly as the deleted `lakeflow_trigger.py` was — but the only tests left are the 5 in `tests/test_config.py`. `tests/test_lakeflow_trigger.py` went with the module it covered and was not replaced.
- `src/agentic_restock/quote_persistence.py` and `integrations/teams_webhook.py` duplicate logic that now lives in `mcp-inventory-actions/server/tools.py` (quote writing, Adaptive Card building, `build_review_app_url`). Only `test_teams_card.py` still imports them, for local card rendering. The MCP server is authoritative; don't add a caller to the local copies.
- `src/agentic_restock/jobs/demo_data.py` is imported by nothing — superseded by `notebooks/simulation/generate_sim_data.py`.
- `mcp-inventory-actions/README.md` + `Claude.md` are the unmodified "MCP Server - Hello World" template and describe example health/user tools, not the three real ones; `restock-review/README.md` + `CLAUDE.md` are likewise untouched AppKit boilerplate. Read the code, not those four files.
- `docs/prd.md` + `docs/architecture.md` describe the original single-layer design and are kept as historical record. `prd_v2.md` is the 4-tier requirements doc behind the 16 deep-analysis functions, and `docs/market_evidence_phase1.md` supersedes both for anything about the current pipeline. `docs/agentic_coarse_check_design.md` is superseded outright.
