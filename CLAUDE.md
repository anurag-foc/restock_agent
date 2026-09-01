# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Inventory Intelligence" / "Manufacturing Inventory Intelligence Engine" — a Databricks **Agent Bricks** pipeline that scans inventory hourly, routes flagged parts through an LLM analysis chain, persists a restock quote, and notifies a human in Microsoft Teams for approval.

Deployment is **exclusively** via Databricks Asset Bundles (`databricks.yml` + `resources/**/*.yml`). No manual notebook uploads, no click-ops job creation — except the Supervisor Agent, which has no DAB resource type yet (see below).

## Commands

```bash
uv sync                                  # install deps into .venv (editable install of src/)
uv run pytest -q                         # unit tests (no Databricks runtime needed)
uv run pytest tests/test_config.py::test_qualified_table_uses_own_catalog_and_schema  # single test
uv run ruff check .

databricks auth login --profile anurag-r # re-auth when the refresh token expires
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run <job_key> -t dev   # job_key = resources.jobs.<key>, e.g. schema_bootstrap
databricks bundle summary -t dev         # also the only way to read the deployed Genie Space id

./scripts/deploy_all.sh dev              # validate → deploy → UC functions → ensure agent → redeploy
                                         # SEED=true also reruns schema_bootstrap (wipes quote_metadata)
```

Always pass `--profile <name>`; never let the CLI pick a default. Targets: `dev` (default) and `prod`, both deploying to `/Workspace/Shared/.bundle/agentic_restock/<target>`.

Scripts that hit the live workspace are run with `PYTHONPATH=src python3 scripts/<x>.py` (they are not pytest tests): `run_e2e_pipeline.py` (full pipeline outside the job), `test_teams_card.py` (card rendering; dry-run without `TEAMS_WEBHOOK_URL`), `validate_genie_groundedness.py` (3 golden scenarios checking Genie reasons rather than echoing snapshot flags).

## Architecture — the invariant that governs everything

**Analysis is always reached through a natural-language interface; writes are always reached through an idempotent action tool.** Deterministic analysis logic lives in **Unity Catalog SQL functions**, which are trusted assets of **Genie Spaces** — the Supervisor never calls those functions directly. Nothing is a Python "agent class."

```
Lakeflow Job (hourly, PAUSED)
  coarse_check       → multi-signal SQL scan, sets candidate_count + candidates_json task values
  has_candidates     → condition task, branches on candidate_count > 0
  invoke_supervisor  → multi-turn conversation with the Supervisor Agent serving endpoint;
                       the agent itself calls persist_quote then send_human_review

restock-review app (Databricks App)
  PM approves/rejects one line → triggers the restock_decision job
    apply_decision     → deterministic status write (no LLM)
    has_approval       → condition task, APPROVED only
    invoke_fulfillment → fresh Supervisor turn: fulfillment_guardrail → fulfill_restock_request

Supervisor Agent (SDK-only, Beta) — exactly three tools:
  genie_agent           genie_space    → 16 UC SQL functions (deep analysis, read-only)
  fulfillment_guardrail genie_space    → fulfillment re-check (read-only)
  inventory_intelligence_actions    app            → custom MCP server, mcp-inventory-actions app
                                         (persist_quote, send_human_review, fulfill_restock_request)
```

**The Supervisor's tool set is exactly those three**, reconciled by `scripts/ensure_supervisor_agent.py`, which deletes anything else it finds. The rule this enforces: **never attach UC functions to the Supervisor directly.** An earlier revision did, and the Supervisor then called them straight from candidate JSON and never invoked Genie at all, defeating the design. Analysis stays behind Genie; only `inventory_intelligence_actions` writes.

**Why the action tools are a custom MCP app and not `uc_function`** (verified, do not re-litigate by trying again):
- A UC **SQL** function body containing `INSERT` fails with `PARSE_SYNTAX_ERROR` — DML is not permitted.
- Hence: a custom MCP server (`mcp-inventory-actions/`, built from Databricks' official "MCP Server - Hello World" app template), attached to the Supervisor directly via the `app` tool type. `app` tool type only accepts an `mcp-`- or `agent-`-prefixed app name, and reaches it via **app authorization** rather than a UC HTTP Connection — no service principal/secret scope to set up for the Supervisor's side. The app's own auto-provisioned service principal still needs Unity Catalog grants on the tables it writes (see `docs/agent_bricks_mapping.md`) — a one-time `GRANT`, not an OAuth/secret-scope dance.

**Every action tool enforces its own idempotency server-side** (`mcp-inventory-actions/server/tools.py`) — the Supervisor is an LLM and may retry or double-call, and a duplicate `fact_restock_request` row is a duplicate procurement order. `persist_quote` derives a deterministic `quote_id` from the candidate set + date; `send_human_review` no-ops if `teams_message_id` is set; `fulfill_restock_request` only acts on a line currently `APPROVED`. The notebooks *verify* the tools ran and fail loudly if not — they never retry them.

Read [docs/agent_bricks_mapping.md](docs/agent_bricks_mapping.md) before touching anything agent-related — it is the authoritative record of why each piece looks the way it does.

### Signal scanner (`src/agentic_restock/jobs/lakeflow_trigger.py`)

Pure string-building function (no Spark import) so it is unit-testable locally. Emits one UNION-ALL query over three signals, deduped per part/warehouse by urgency rank:

- `STOCK_THRESHOLD` (CRITICAL) — reactive: `current_stock <= safety_stock`
- `PREDICTED_STOCKOUT` (HIGH) — proactive: `predicted_stockout_date()` within 14 days
- `BOM_CASCADE_RISK` (HIGH) — component shortfall threatening an ASSEMBLY/SUB-ASSEMBLY parent

`fact_inventory_snapshot` is a *daily* fact, so every read takes the latest `SNAPSHOT_DATE_KEY` per `(PART_KEY, WAREHOUSE_KEY)` via `ROW_NUMBER()`. There is no `reorder_point`/`is_active` config table — `SAFETY_STOCK_QTY` is the reorder trigger and `MAX_STOCK_LEVEL` the restock target, both columns on the snapshot fact.

### Review App (`restock-review/`)

An AppKit (Node/React) Databricks App, deployed as part of the same bundle (`resources/apps/restock_review_app.yml`, `source_code_path: ../../restock-review`). **UI-only** — `pending_quotes` / `quote_header` / `quote_lines` SQL queries under `config/queries/`, rendering a quote and letting a PM approve or reject **each part-line independently** — `fact_restock_request`'s grain is one row per part-line, and `REQUEST_STATUS_KEY`/`DECISION_DATE_KEY`/`CONFIRMED_QTY` are all per-line. The decision endpoint only pre-validates then triggers the `restock_decision` job; it does not write. It no longer hosts the Supervisor's action tools (see below).

Analytics caching is explicitly **disabled** (`cache: { enabled: false }` in `server/server.ts`). The default shared cache served stale rows after a decision was written, which on an approval screen means showing a PM that a line they just approved is still pending.

Local dev: `npm run dev` (port 8000). `useAnalyticsQuery` has no `refetch()`, so the UI forces a refresh by remounting via a changing `key`. Analytics query params must be wrapped (`sql.string(...)`) — the wire format is `{"__sql_type":"STRING","value":"..."}`, and a bare string is rejected server-side.

### Action MCP server (`mcp-inventory-actions/`)

A Python Databricks App built from Databricks' official "MCP Server - Hello World" template (FastMCP + FastAPI), deployed via `resources/apps/mcp_inventory_actions_app.yml`. Exposes `persist_quote`, `send_human_review`, `fulfill_restock_request` as MCP tools (`server/tools.py`), each idempotent by construction (see above). Runs SQL via the app's own service-principal-authenticated `WorkspaceClient` (`server/db.py`, `server/utils.py::get_workspace_client`) against `DATABRICKS_WAREHOUSE_ID` — not on-behalf-of-user auth, since the caller is the Supervisor Agent, not an interactive user. The app's service principal needs `USE CATALOG`/`USE SCHEMA`/`SELECT` on `gold_dev.dim` and `gold_dev.supply_chain_analytics`, plus `INSERT, UPDATE` on `fact_restock_request` and `quote_metadata`, plus `CAN_USE` on the SQL warehouse — grant these once after first deploy; a missing grant surfaces as a silent SQL failure inside a tool call, not an auth error, since app authorization to the Supervisor succeeds regardless.

### Supervisor invocation (`notebooks/lakeflow_trigger/invoke_supervisor.py`)

Several hard-won details live here; changing them tends to break things silently:

- The endpoint speaks the OpenAI **Responses API** shape (`{"input": [{"role", "content"}]}`), **not** Chat Completions. `serving_endpoints.query()` builds the wrong shape, so this calls `w.api_client.do("POST", "/serving-endpoints/{name}/invocations", ...)` directly. The reply text is buried in `response["output"][…]["content"][…]["output_text"]`.
- Timeouts must be passed as `WorkspaceClient(config=Config(http_timeout_seconds=..., retry_timeout_seconds=...))`. `WorkspaceClient()` rejects them as kwargs, and setting `w.config.*` after construction is read too late and silently has no effect — the symptom is `TimeoutError: Timed out after 0:05:00`.
- Model Serving has a **290s HTTP gateway ceiling**. Hence the multi-turn protocol: turn 0 briefing → one turn per candidate (each request resets the clock, ~60–80s) → optional history compression above `COMPRESSION_THRESHOLD = 4` candidates → a final synthesis turn that passes the per-candidate reports explicitly and forbids further Genie calls. Do not collapse this back into one big prompt.

### UC SQL functions (`notebooks/uc_functions/deep_analysis_functions.ipynb`)

Deployed by the on-demand `deploy_uc_functions` job (`CREATE OR REPLACE`, idempotent). Plain SQL, not Python UDFs, so they stay queryable from a SQL editor for debugging. Two Spark constraints bite constantly:

- Internal calls between functions must be **fully qualified** (`gold_dev.supply_chain_analytics.avg_daily_consumption(...)`) or `CREATE FUNCTION` fails to resolve them.
- A **scalar** function whose body scans a table and filters to one row must wrap the result in `MAX(...)`. Spark's validator rejects correlated scalar subqueries it cannot prove return exactly one row, even when filtering on a composite primary key. Table-valued (`RETURNS TABLE`) functions are exempt and can use `WHERE`/`QUALIFY ROW_NUMBER()`.

There is deliberately **no single `needs_restock` boolean**. The restock veto is Genie's actual reasoning job; it composes it by comparing `requested_restock_qty` against `pending_procurement_qty` and can cite the specific PO via `open_procurement_orders`. Resist collapsing multi-step judgment into one opaque function — the same principle keeps `restock_candidate_summary` scoped to one canned phrasing.

## Data layer

`src/agentic_restock/config.py` is the single source of truth for catalog/schema/table names, overridable by `AGENTIC_RESTOCK_{GOLD_CATALOG,DIM_SCHEMA,FACTS_SCHEMA,CATALOG,SCHEMA}`. Import from it rather than hardcoding names.

- `gold_dev.supply_chain_analytics` — Data Engineering's facts: `fact_inventory_snapshot`, `fact_inventory_transaction` (`ISSUE` rows = consumption), `fact_procurement`, `fact_restock_request`. Read-only **except** `fact_restock_request`, which the quote writer appends to.
- `gold_dev.dim` — `dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`, `dim_request_status`. Business keys (`PART_ID`) ↔ surrogate keys (`PART_KEY`); dimension joins need `IS_CURRENT = true`.
- `quote_metadata` — ours: Teams/Review-App fields per quote (`summary_report`, `teams_message_id`, `databricks_preview_url`) that have no home in `fact_restock_request` (whose grain is one row per part-line, not per quote).

Real inventory data is DE-managed and never seeded by this repo; `schema_bootstrap` only touches `quote_metadata`.

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

- `tests/test_lakeflow_trigger.py::test_query_filters_active_below_safety_stock` **fails**: it asserts the retired `ls.QUANTITY_ON_HAND <= ls.SAFETY_STOCK_QTY` predicate, which the multi-signal rewrite replaced with `current_stock_qty <= reorder_point_qty` in the `s1_threshold` CTE.
- The UC functions moved to `gold_dev.supply_chain_analytics` and grew from 9 to 16 (adding the Tier 1–4 intelligence functions from `prd_v2.md`). `README.md`, `docs/agent_bricks_mapping.md`, `docs/uc_functions_reference.md`, and the job descriptions still say `ab_training.agentic_restock` and list the old 9.
- `create_supervisor_agent.py` creates its Genie tool as `inventory_intelligence_engine`; `ensure_supervisor_agent.py` expects `genie_agent` and deletes everything else — so running the former then the latter deletes and recreates the tool under a different id.
- `docs/prd.md` + `docs/architecture.md` describe the original single-layer design; `prd_v2.md` is the current 4-tier (forecasting / procurement / manufacturing / financial) requirements doc that the Supervisor instructions and newer UC functions implement.
