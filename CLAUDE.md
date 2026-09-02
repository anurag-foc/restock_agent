# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Inventory Intelligence" / "Manufacturing Inventory Intelligence Engine" — a Databricks **Agent Bricks** pipeline that rebuilds a full inventory signal board twice a day, has an LLM pick and analyse the highest-decision-value action for each live signal type (typically 1-3: `STOCK_THRESHOLD`, `BOM_CASCADE_RISK`, `STALLED_COMMITMENT`), bundles them into one restock quote, and notifies a human in Microsoft Teams for approval.

The "a handful of actions, twice a day, bundled into one notification" shape is deliberate and is the product thesis, not a limitation: `docs/market_evidence_phase1.md` §3 is the argument that an hourly cadence emitting every flagged part reproduces the alert fatigue this product exists to fix. That section's own recommendation is "a hard output budget... ranked by money" — a bounded, ≤3-4-line, single-notification quote is exactly that, not a departure from it. Read that doc plus [docs/end_to_end_walkthrough.md](docs/end_to_end_walkthrough.md) (the plain-language walk-through of the current pipeline) before proposing a design change.

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
databricks bundle summary -t dev         # also the only way to read the deployed Genie Space id

./scripts/deploy_all.sh dev              # validate → deploy → UC functions → ensure agent → redeploy
                                         # SEED=true also reruns schema_bootstrap (wipes quote_metadata)
```

Always pass `--profile <name>`; never let the CLI pick a default. Targets: `dev` (default) and `prod`, both deploying to `/Workspace/Shared/.bundle/agentic_restock/<target>`.

Scripts that hit the live workspace are run with `PYTHONPATH=src python3 scripts/<x>.py` (they are not pytest tests): `run_e2e_pipeline.py` (the full pipeline outside the job — mirrors `invoke_supervisor.py`'s turn protocol: pre-check, Turn 1, one analysis turn per live signal type, final persist/notify turn; `--dry-run` stops before the Supervisor call, `--skip-board-refresh` reuses the existing board), `test_teams_card.py` (card rendering; dry-run without `TEAMS_WEBHOOK_URL`), `validate_genie_groundedness.py` (3 golden scenarios checking Genie reasons rather than echoing snapshot flags), `add_restock_note_column.py` (one-off idempotent `ALTER TABLE` adding `fact_restock_request.NOTE`, which holds the PM's free-text approve/reject reasoning), `seed_demo_scenarios.py` (the centralized demo data feeder — idempotently seeds `dim_bom`/`dim_supplier_contract`/`fact_supplier_delivery` with a small, curated set of intelligence scenarios so every signal type has a live example, plus two deterministic **coverage generators** described below; `--report` prints a layer-readiness checklist instead of seeding; `--rebuild-facts` is DESTRUCTIVE and replaces `fact_inventory_snapshot`/`fact_inventory_transaction`/`fact_procurement` wholesale with this same hand-curated, fully-attributable dataset — see the script's module docstring for what it preserves and why `generate_sim_data` was retired in its favor).

## Architecture — the invariant that governs everything

**Analysis is always reached through a natural-language interface; writes are always reached through an idempotent action tool.** Deterministic analysis logic lives in **Unity Catalog SQL functions**, which are trusted assets of **Genie Spaces** — the Supervisor never calls those functions directly. Nothing is a Python "agent class."

```
Lakeflow Job (07:00 + 15:00 UTC, PAUSED)
  refresh_signal_board → CREATE OR REPLACE inventory_signal_board: one row per
                         (part, warehouse), every phase-1 nuance as a column
  invoke_supervisor    → COUNT(*) over rank_priority_actions(5); exits NO_ACTION at 0.
                         Otherwise a 2+N-turn conversation with the Supervisor endpoint
                         (N = distinct live signal types, typically 1-3): Turn 1 scans
                         rank_priority_actions_diverse, one turn analyses each candidate,
                         a final turn calls persist_quote (all N lines, one quote_id)
                         then send_human_review (one Teams card)

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

The eight phase-1 UC functions, all reading the board: `scan_transfer_options`, `scan_assembly_risk`, `rank_priority_actions`, `rank_priority_actions_diverse`, `scan_demand_shift`, `scan_leadtime_drift`, `evaluate_suppliers`, `evaluate_feasibility`. Deployed by `deploy_uc_functions`' third task.

`rank_priority_actions_diverse` reuses `rank_priority_actions`' exact ranking (same formula, same suppression), but partitions by `signal_type` instead of taking one global top-N — it returns the single top-ranked row for EACH distinct signal type currently live, naturally bounded by how many signal types the CASE expression can produce (3 today: `STOCK_THRESHOLD`, `BOM_CASCADE_RISK`, `STALLED_COMMITMENT`). This is what `invoke_supervisor.py`'s Turn 1 calls so a run surfaces the best example of each intelligence layer, not just the single loudest number, while staying bundled into one quote and one Teams notification.

`rank_priority_actions` is the one that matters: `decision_value = GREATEST(exposure - action_cost, 0)`, where `action_cost` scales with how expensive the cheapest viable fix is (≈3% of exposure if a transfer covers it, 15–50% scaled by lead time if only a buy exists, 100% if nothing helps). The formula is intentionally simple and visible **so it can be argued with** — do not hide it behind a black box. Whether decision-value ranking actually differs from raw exposure ordering on real data is still an open validation item (§16).

**Suppression (nuance 8) lives inside `rank_priority_actions` as a `WHERE` clause**, not in Genie's instructions — an LLM that remembers to filter 97% of the time re-raises a rejected item about monthly. An open commitment suppresses the row only while it is *fresh*: `PENDING_APPROVAL`/`NEEDS_REVIEW` re-surface after 2 days, `APPROVED`/`FULFILLING` after `effective_lead_days + 3`, tagged `STALLED_COMMITMENT`, because their exposure keeps accruing while they sit. `REJECTED` stays permanently suppressed. Known simplification: re-surfacing is time-based only; "exposure grew 1.5x since the decision" needs an `EXPOSURE_AT_DECISION` captured at decision time, which `fact_restock_request` does not store.

Several of the older 16 deep-analysis functions are now redundant with board columns (`classify_urgency`, `predicted_stockout_date`, `dynamic_reorder_point`, `consumption_anomaly_score`, …), and the three narrative-STRING ones (`assembly_risk_report`, `financial_tradeoff_summary`, `plant_capacity_check`) were a workaround for having no LLM in the loop — Genie writes the sentence now. **Audited** (previously an open item — `fulfillment_guardrail`'s Genie Space and the fulfillment path had never been checked for dependence on them): `fulfillment_guardrail` depends on exactly 4 of the 16 (`pending_procurement_qty`, `requested_restock_qty`, `predicted_stockout_date`, `avg_daily_consumption`) via its own independent trusted-asset list — those stay. The other 12 are used by neither `fulfillment_guardrail` nor any Supervisor turn prompt nor any of `genie_agent`'s own sample questions/example SQLs, so they've been removed from `genie_agent`'s trusted-asset list (pure noise in the Supervisor's per-turn tool-selection surface); the UC functions themselves are untouched (`CREATE OR REPLACE FUNCTION` stays in `deep_analysis_functions.ipynb`) for ad-hoc SQL-editor debugging.

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

**The Teams card is deliberately a nudge, not the report.** `_build_adaptive_card` parses `summary_report` into its `## CANDIDATE i of N` blocks and emits a `Found N action items` header plus one bold line per item (the `RECOMMENDATION`) with a subtle second line (`Rs <decision value> at stake · <first sentence of WHY NOW>`), then a plain `Review in Databricks` button. It used to dump the first 1200 characters of the raw report onto the card, which nobody reads on a phone — options considered, evidence, and the if-approved-and-wrong arithmetic all live in the Review App, which is where a decision is actually made. A report with no `## CANDIDATE` markers (every quote written before the multi-candidate protocol) degrades to a single item rather than rendering empty.

### Report grounding rules (`SUPERVISOR_INSTRUCTIONS` in `scripts/create_supervisor_agent.py`)

Every figure the PM reads has to be one they can check against something. Two rules in the agent
instructions exist because the LLM broke them on live runs, both times by reaching for the ranking's
internal `action_cost` and presenting it as a real cost:

- **A purchase's `IF APPROVED AND WRONG` is written left to right as a computation, with the total
  LAST** — `200 x Rs 3,200 = Rs 6,40,000 plus Rs 4,160 holding = Rs 6,44,160 spent if …`. Two earlier
  attempts put the total first and the breakdown in a parenthetical after it, and both times the
  total was `action_cost` (`exposure × (0.15 + lead_days/90 × 0.35)`) while only the breakdown was
  real: `Rs 28,90,625 (150 x Rs 3,400 + Rs 6,120)` sums to `Rs 5,16,120`, 5.6x off, and after the
  first fix, `Rs 94,580 (200 x Rs 3,200 = Rs 6,40,000, plus Rs 4,160)` was `action_cost + holding`
  next to a breakdown summing to `Rs 6,44,160`. Prose telling it to check the sum did not hold; a
  template with no headline slot to fill in ahead of the arithmetic did. The same total is also
  what `OPTIONS CONSIDERED`'s `[CHOSEN]` cost field must carry — that field was `action_cost` too.
- **A transfer's `IF APPROVED AND WRONG` has no rupee figure at all**, and neither does its
  `OPTIONS CONSIDERED` cost field (the literal words `no purchase`). Moving owned stock between
  warehouses spends nothing, and there is no freight or handling cost anywhere in this data — so the
  next run, told to state "the real handling/freight cost, not zero", back-solved
  `Rs 17,280 (80 x Rs 216)` from `action_cost = exposure × 0.03`. `Rs 216` exists nowhere. The line
  now states the actual downside instead: the donor drops to `donor_cover_after_days` days of cover
  for nothing.

- **`action_cost` is no longer printed as a rupee figure anywhere a PM reads.** `DECISION VALUE` used
  to render as `Rs <dv> (exposure Rs <exposure> less Rs <action_cost> to act)`; it is now
  `Rs <dv> (Rs <exposure> at risk, ranked after allowing for how expensive the cheapest fix is)`.
  Keeping it on screen was both the thing the model kept reaching for and a claim a PM cannot check
  against any quote — it is a ranking heuristic, not a price. The formula itself stays visible and
  arguable in `priority_functions.py` and in `rank_priority_actions`' output; it just no longer
  appears on the card. `IntelligenceReport.tsx` parses both shapes (every quote written before this
  change still has the old one) and its `DecisionValueBar` needs only `decisionValue`/`exposure`.

The general failure mode to watch for: `action_cost` and `decision_value` are computed for *every*
candidate before any option is chosen, so they are always available and always plausible-looking, and
an instruction that demands a cost figure where none exists gets one manufactured rather than
refused. Where a figure genuinely does not exist, say so in words instead of asking for a number. A third rule follows the same logic — a failed or empty tool call is never evidence, because
`no surplus available (query failed)` tells a PM two contradictory things and they cannot tell "we
looked and there is none" from "we could not look".

### Supervisor invocation (`notebooks/lakeflow_trigger/invoke_supervisor.py`)

Several hard-won details live here; changing them tends to break things silently:

- The endpoint speaks the OpenAI **Responses API** shape (`{"input": [{"role", "content"}]}`), **not** Chat Completions. `serving_endpoints.query()` builds the wrong shape, so this calls `w.api_client.do("POST", "/serving-endpoints/{name}/invocations", ...)` directly. The reply text is buried in `response["output"][…]["content"][…]["output_text"]`.
- Timeouts must be passed as `WorkspaceClient(config=Config(http_timeout_seconds=..., retry_timeout_seconds=...))`. `WorkspaceClient()` rejects them as kwargs, and setting `w.config.*` after construction is read too late and silently has no effect — the symptom is `TimeoutError: Timed out after 0:05:00`.
- Model Serving has a **290s HTTP gateway ceiling** (per-turn timeout is set to 280s). Hence the multi-turn protocol, each request resetting the clock: a **pre-check** counts distinct live signal types (`COUNT(*)` over `rank_priority_actions_diverse()`) to get `N` (typically 1-3) · **Turn 1** call `rank_priority_actions_diverse` and list the top action per signal type, nothing else · **Turns 2.1..2.N**, one round-trip per candidate, each drilling down with one or two of the six scan/evaluate functions — plus `scan_demand_shift` and `scan_leadtime_drift` **always**, since those two are corrections to the burn rate and lead time every other figure in the artifact rests on, and the prompt requires an explicit one-line EVIDENCE statement even when they return nothing — and writing an OUTPUT CONTRACT artifact tagged `## CANDIDATE i of N` · a **final turn** calls `persist_quote` with all `N` candidates in one array (writing `N` `fact_restock_request` lines under one `quote_id`) then `send_human_review` once. A turn that also drills into a candidate (multiple further Genie calls) plus writes up the analysis was tried combined with the scan first and reliably timed out — ranking plus drill-downs plus the write-up exceeds the ceiling. Do not merge them back; doing N candidates as N separate round-trips costs wall-clock, not timeout risk.
- **Custom MCP tool calls require an explicit approval round-trip.** Any `app`-tool-type call comes back as an `mcp_approval_request` item instead of executing, and there is no way to disable that at registration or per-request time (both checked, neither honored). The endpoint is **stateless**, so `previous_response_id` chaining does *not* work — it returns `"Invalid message sequence. The approval response was in an unexpected position."` Continuing means resending the whole transcript: everything sent so far, plus every item from the prior response's `output` **verbatim**, plus an `mcp_approval_response`. `_invoke()` answers it inline within the same turn, bounded by `MAX_APPROVAL_ROUNDS = 5`. The same helper is duplicated in `invoke_fulfillment.py` and `scripts/run_e2e_pipeline.py`; fix all three together.
- **`invoke_supervisor.py` never reads the ranked rows** — only `COUNT(*)` (twice: once over `rank_priority_actions(5)` for the NO_ACTION gate, once over `rank_priority_actions_diverse()` for the turn-loop count `N`). That is structural, not stylistic: an earlier revision handed the Supervisor pre-chewed candidate JSON and it reasoned straight from that, never calling Genie. The only way to keep Genie on the critical path is to genuinely not know the answer here. A `COUNT(*)` is a boolean/count fact ("is there work", "how many"), not a judgment about which candidates matter.
- **`persist_quote`'s signature pre-dates the phase-1 redesign** — it still expects `item_id, warehouse_id, current_stock_qty, reorder_point_qty, suggested_reorder_qty, initial_urgency`, not `rank_priority_actions`' output shape. Rather than a shim, the final turn tells the Supervisor to assemble `candidates_json` from the board itself. It already loops over an arbitrary-length `candidates_json` array and writes one `fact_restock_request` line per candidate under one shared `quote_id` — no tool-side change was needed to support multiple candidates in one quote. Updating the tool's contract to accept the action shape natively is a real open follow-up.

### UC SQL functions

**24 functions total in `gold_dev.supply_chain_analytics`**, in two generations, **no longer all attached to `genie_agent` as trusted assets** (see "Known drift" below for the audit that changed this):
- **16 deep-analysis functions** — `notebooks/uc_functions/deep_analysis_functions.ipynb`, the Tier 1–4 intelligence set from `prd_v2.md`. Reference: [docs/uc_functions_reference.md](docs/uc_functions_reference.md). Only 4 of these (`pending_procurement_qty`, `requested_restock_qty`, `predicted_stockout_date`, `avg_daily_consumption`) are still attached to a Genie Space — `fulfillment_guardrail`, which genuinely depends on them. The other 12 are unattached anywhere (still live in Unity Catalog for ad-hoc SQL-editor debugging, just not Genie-callable).
- **8 phase-1 priority functions** — `notebooks/uc_functions/priority_functions.py` over `src/.../priority_functions.py` (see above), all 8 attached to `genie_agent`.

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
- `dim_bom`, `dim_supplier_contract` — named in `config.py` and read by the board query (BOM cascade, contracted lead times). Grown by `scripts/seed_demo_scenarios.py`'s default (idempotent, additive) mode.
- `fact_plant_capacity` — named in `config.py` but **not actually read by anything** (confirmed by grep; only the retired legacy `plant_capacity_check` function ever touched it, and its `PLANT_ID` values never even matched `dim_plant`'s real IDs). Cleared by `--rebuild-facts` and deliberately left empty.
- `sim_events`, `sim_pair_scenarios` — existed to give `generate_sim_data`'s randomized output backtest attribution (which single planted scenario produced which row). Retired along with `generate_sim_data` (see below) and left empty — a hand-curated dataset with no randomness has nothing to attribute.

**`generate_sim_data` (the job and `notebooks/simulation/generate_sim_data.py`) has been retired and deleted.** Its dependency, `src/agentic_restock/simulation.py`, went missing from the repo (never in git history, not deployed) while the ~68K/63K-row snapshot/transaction data it had already produced kept working — so the generator itself could no longer be re-run, only its stale output remained. Rather than restore that opaque module, `fact_inventory_snapshot`/`fact_inventory_transaction`/`fact_procurement` are now populated by `scripts/seed_demo_scenarios.py --rebuild-facts`: a **fully hand-authored, curated dataset** (this repo's, not a simulator's) where every row exists for a known reason — a BOM cascade, a lead-time-drift supplier, a demand-shift spike, or a healthy background part — rather than being an opaque simulation output. This is a real invariant change: real inventory data used to be exclusively DE-managed and only `generate_sim_data` (as an explicit, documented exception) could write to it; in `dev`, DE-owned-shaped facts are now this repo's hand-curated data instead. `--rebuild-facts` is DESTRUCTIVE (`DELETE`, not `DROP` — the table DDL stays Data Engineering's) and preserves every `(part, warehouse)` combination the BOM cascades, lead-time-drift contracts, and existing `fact_restock_request` lines depend on — see the script's module docstring for the exact scenario list and the trade-offs accepted (no `sim_pair_scenarios`-style backtest attribution; a much smaller, curated board — around 45 rows — rather than 226 opaque ones).

**Named examples are not enough on their own — hence the two coverage generators.** The
hand-authored rows give each nuance one explainable example, but `rank_priority_actions_diverse`
surfaces whichever part currently ranks top of its signal type, and that part rotates (a quote
raised on today's winner suppresses it for 2 days, so tomorrow's winner is someone else). A nuance
the *winner* has no data for is a nuance that never reaches a live report. The first multi-candidate
run hit exactly that: 5 of 44 (part, warehouse) pairs had any transaction history and every
contracted supplier had **one** on-time delivery on file — which averages to a 0.0-day drift and
reads as "on contract" while actually meaning "no record" — so `scan_demand_shift` and
`scan_leadtime_drift` were structurally blind for almost the whole working set. Two generators in
`seed_demo_scenarios.py` fix that, both deterministic (assignment fixed by sorted position, never
random) so a rebuild reproduces the same dataset:

- `_generate_consumption_history` — ISSUE rows for every pair with a real burn rate, volumes derived
  from that pair's own `AVG_DAILY_CONSUMPTION` so the trailing-365d rate agrees with the snapshot
  average the board already carries. Two thirds of pairs get a multiplier outside
  `scan_demand_shift`'s 0.8–1.2 band (1.5 spike / 0.65 drop / 1.0 flat, round-robin).
- `_generate_supplier_delivery_rows` — six deliveries for each contracted supplier with **fewer than
  three** on file (3, not 1, for the reason above); every other supplier by sorted id drifts 5–6
  days, the rest deliver on contract. Suppliers with a real record keep it untouched.

Deliberately not "make every part drift": a dataset where every supplier is late and every part is
seasonal makes both signals meaningless. About half the working set genuinely has no lead-time drift,
and Turn 2's prompt requires the report to *say* that rather than omit the line.

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
- **Thin test coverage on the phase-1 code.** `signal_board.py`, `priority_functions.py` and `run_log.py` are pure string-builders — locally testable by design, exactly as the deleted `lakeflow_trigger.py` was — but only `priority_functions.py` has any coverage (`tests/test_priority_functions.py`, 3 tests on `FUNCTION_NAMES`/`build_function_statements` alignment), alongside the 5 in `tests/test_config.py` and the 6 in `tests/test_teams_card_builder.py` (which reaches into `mcp-inventory-actions/` via `sys.path` and `importorskip`, since the card builder is pure dict work and the MCP app has no suite of its own). `signal_board.py` and `run_log.py` still have none. `tests/test_lakeflow_trigger.py` went with the module it covered and was not replaced.
- `src/agentic_restock/quote_persistence.py` and `integrations/teams_webhook.py` duplicate logic that now lives in `mcp-inventory-actions/server/tools.py` (quote writing, Adaptive Card building, `build_review_app_url`). Only `scripts/test_teams_card.py` still imports them, for local card rendering — and it now renders a card shape Teams no longer receives, since the simplified `Found N action items` card was implemented in the MCP server only. Use `tests/test_teams_card_builder.py` to check the real card. The MCP server is authoritative; don't add a caller to the local copies.
- `mcp-inventory-actions/README.md` + `Claude.md` are the unmodified "MCP Server - Hello World" template and describe example health/user tools, not the three real ones; `restock-review/README.md` + `CLAUDE.md` are likewise untouched AppKit boilerplate. Read the code, not those four files.
- `docs/prd.md` + `docs/architecture.md` describe the original single-layer design and are kept as historical record. `prd_v2.md` is the 4-tier requirements doc behind the 16 deep-analysis functions, and `docs/market_evidence_phase1.md` supersedes both for anything about the current pipeline. `docs/agentic_coarse_check_design.md` is superseded outright.
