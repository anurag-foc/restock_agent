# Agent Bricks Mapping

How the architecture in [`docs/architecture.md`](architecture.md) maps onto real
**Databricks Agent Bricks** primitives, what was actually built, and why the
implementation looks different from a naive "one Python class per agent"
reading of the architecture doc.

---

## 1. The core correction

The architecture doc's §2 table names four "agents" (Lakeflow Job, Supervisor
Agent, Genie Agent, Restock Agent) as if they were peer components you'd each
write as a Python class. Agent Bricks doesn't work that way:

| Architecture doc term | Is actually... | Not... |
|---|---|---|
| **Genie Agent** | A **Genie Space** — a natural-language interface over a fixed set of UC tables, configured with instructions, example SQL, and **trusted UC functions as tools**. It has no "code" of its own; all deterministic logic lives in the UC functions it's allowed to call. | A Python module with `avg_daily_consumption()` etc. as methods. |
| **Supervisor Agent** | A **Supervisor Agent** (Mosaic AI Agent Bricks, currently Beta/SDK-only) — an LLM-orchestrated router. Deliberately holds a single tool — the Genie Space above — so every analysis question is forced through Genie's natural-language interface, never computed directly. | A hand-written orchestration graph (LangGraph-style) that we control step-by-step, or an agent with direct database/function access that bypasses Genie. |
| **"Deep analysis" logic** (§4.2: avg consumption, stockout forecast, urgency, quote text) | **Unity Catalog SQL functions** — governed, reusable, independently testable/queryable tools attached to the Genie Space *only*. | Python functions run inside a notebook task, or tools attached directly to the Supervisor Agent. |
| **Restock Agent** | Not yet built — will be either another Supervisor Agent tool (a UC function or a small agent) invoked after human approval. | — |

The key mental-model shift: **the deterministic work (§4.2 formulas) is not
"inside" any agent — it's UC functions the Genie Space calls as tools.**
The Supervisor Agent has no direct line to them at all; it only ever reaches
them by asking the Genie Space a natural-language question. (An earlier
revision attached the same functions to the Supervisor Agent directly too, so
it could skip Genie when it already had exact `part_id`/`warehouse_id` keys —
that's since been removed. See §2.4.)

---

## 2. Component-by-component mapping

### 2.1 Lakeflow Job → Databricks Job (unchanged)

Architecture §4.1's coarse check is a plain Databricks Job — no Agent Bricks
primitive needed here, it's cheap deterministic SQL and Agent Bricks would be
overkill.

- **Resource:** `resources/jobs/lakeflow_trigger_job.yml` — `lakeflow_trigger` job, hourly, `PAUSED` by default.
- **Tasks:**
  1. `coarse_check` (`notebooks/lakeflow_trigger/coarse_check.py`) — runs `src/agentic_restock/jobs/lakeflow_trigger.py`'s SQL, sets `candidate_count` / `candidates_json` task values.
  2. `has_candidates` — condition task, branches on `candidate_count > 0`.
  3. `invoke_supervisor` (`notebooks/lakeflow_trigger/invoke_supervisor.py`) — only runs on the `true` branch; calls the Supervisor Agent endpoint (see §2.3) via `job.parameters.supervisor_endpoint_name`.

### 2.2 Genie Agent → Genie Space

A **Genie Space** (Databricks' natural-language-to-SQL product) configured over the three source tables, with the §4.2 UC functions as **trusted assets** so it can call them like any other column/table when answering.

- **Data sources:** Data Engineering's `gold_dev` star schema only — `fact_inventory_snapshot`, `fact_inventory_transaction`, `fact_procurement`, `fact_restock_request` (all in `gold_dev.supply_chain_analytics`), plus `dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`, `dim_request_status` (`gold_dev.dim`). Deliberately excludes `ab_training.agentic_restock.quote_metadata` — that table is Teams/Review-App bookkeeping for already-created quotes, which is the Supervisor Agent's concern, not Genie's pre-quote analysis scope.
- **Trusted UC functions:** all six functions from §2.3 below — this is how Genie gets access to "avg daily consumption" and "urgency" as first-class concepts instead of trying to reason about them from raw SQL.
- **Text instructions:** one consolidated instruction block telling Genie to prefer the UC functions over hand-rolled SQL for consumption/forecast/urgency questions, and to interpret "needs restocking" via the `QUANTITY_ON_HAND <= SAFETY_STOCK_QTY` rule from §4.1 (on the latest `fact_inventory_snapshot` row per part/warehouse).
- **Example SQL:** a handful of `example_question_sqls` pairing natural-language questions with the exact SQL (including UC function calls) Genie should produce.
- **Resource:** `resources/genie/genie_agent.genie_space.yml` (DAB `genie_spaces` resource, requires `bundle: engine: direct` in `databricks.yml`) + `notebooks/genie/genie_agent.geniespace.json` (the serialized space config, round-tripped via `databricks bundle generate genie-space`).
- **Why DAB works here:** unlike Supervisor Agents (see below), Genie Spaces *do* have a first-class `genie_spaces` DAB resource type, so this one is fully IaC — `databricks bundle deploy` creates/updates it.

### 2.3 §4.2 deep-analysis logic → Unity Catalog SQL functions

Nine UC functions in `ab_training.agentic_restock` (the schema we own), defined in
`notebooks/uc_functions/deep_analysis_functions.ipynb` and deployed via the
on-demand `resources/jobs/uc_functions_job.yml` (`deploy_uc_functions` job).
Their bodies read Data Engineering's real `gold_dev` star schema. See
[`docs/uc_functions_reference.md`](uc_functions_reference.md) for the full
per-function reference (signature, use case, edge cases, examples) — the
table below is just a summary.

| Function | §4.2 formula it implements |
|---|---|
| `avg_daily_consumption(part_id, warehouse_id, lookback_days=14)` | Trailing-window average consumption, from `fact_inventory_transaction` `ISSUE` rows |
| `predicted_stockout_date(part_id, warehouse_id)` | `today + latest QUANTITY_ON_HAND / avg_daily_consumption` |
| `classify_urgency(stockout_risk, days_remaining)` | CRITICAL/HIGH/MEDIUM/LOW thresholds (CRITICAL also triggered by `fact_inventory_snapshot.STOCKOUT_RISK = 'HIGH'`) |
| `requested_restock_qty(part_id, warehouse_id)` | `MAX_STOCK_LEVEL - QUANTITY_ON_HAND` (latest snapshot row), floored at 0 |
| `pending_procurement_qty(part_id, warehouse_id)` | Restock-veto *input* (scalar): `SUM(PENDING_QTY)` across open (ISSUED/PARTIAL) POs in `fact_procurement` at the warehouse's linked plant |
| `open_procurement_orders(part_id, warehouse_id)` | Restock-veto *input* (table-valued): the row-level open POs behind `pending_procurement_qty` — PO id, supplier, expected date |
| `restock_candidate_summary(part_id, warehouse_id)` | Natural-language quote/assumption text for one candidate — canned template, scoped to the literal "why does X need restocking" phrasing only |
| `avg_lead_time_days(part_id)` | Empirical average supplier lead time derived from `fact_procurement` (`EXPECTED_DATE_KEY - ORDER_DATE_KEY`), since `gold_dev` has no fixed lead-time config field |
| `latest_snapshot(part_id, warehouse_id)` | The single most recent `fact_inventory_snapshot` row (deduped on `SNAPSHOT_DATE_KEY`), so ad-hoc "what's the current stock" questions can't skip that dedup |

**Design note — no single `needs_restock` boolean function:** an earlier
revision had one (`needs_restock(part_id, warehouse_id) → BOOLEAN`), but the
restock veto (§4, step 2) is the one piece of reasoning that *is* the Genie
Agent's actual job — collapsing it into an opaque function let Genie only
echo `TRUE`/`FALSE`, never explain whether it was fully covered, partially
covered, or unconfirmable (no linked plant). It was split into the two
atomic inputs above; Genie now composes the veto itself by comparing
`requested_restock_qty` against `pending_procurement_qty` (see the Genie
Space's `text_instructions` in §2.2's `geniespace.json`), and can cite the
specific PO via `open_procurement_orders` when asked to explain its
reasoning. The same "don't pre-decide Genie's job" principle narrows
`restock_candidate_summary`'s scope: it stays as a convenience template for
one exact phrasing (and the Teams/`quote_metadata.summary_report` text
source), but comparisons, batches, and "what-if" questions are expected to
be composed from the atomic functions instead of forced through it.

These are plain SQL (not Python UDFs) so they're queryable directly from a SQL
editor/notebook for debugging, and so Genie can call them without any
serialization overhead. They are attached to the Genie Space **only** — the
Supervisor Agent has no direct tool access to any of them (see §2.4); it must
go through Genie's natural-language interface for every analysis question.
Implementation quirks worth knowing if you touch these:

- Internal calls between functions must be **fully qualified**
  (`ab_training.agentic_restock.avg_daily_consumption(...)`), not just the bare
  function name, or `CREATE FUNCTION` fails to resolve them.
- Any *scalar* function body that does a **table scan + filter down to one
  row** (e.g. `WHERE item_id = X AND warehouse_id = Y`) must wrap the result
  in an aggregate (`MAX(...)`) — Spark's SQL function validator rejects
  correlated scalar subqueries that it can't prove return exactly one row,
  even when the filter is on a composite primary key. Table-valued functions
  (`RETURNS TABLE (...)`, e.g. `open_procurement_orders`, `latest_snapshot`)
  are exempt from this since they're allowed to return multiple rows; they
  can use plain `WHERE`/`QUALIFY ROW_NUMBER()` filtering instead.

### 2.4 Supervisor Agent → Supervisor Agent (Beta, SDK-only)

- **What it is:** `Inventory Intelligence - Supervisor Agent`, created via the Databricks SDK's `w.supervisor_agents` service (endpoint name changes across re-creations — kept in sync automatically, see §2.5 step 5).
- **Tools attached:** exactly one — `genie_agent`, a `genie_space` tool pointing at the Genie Space from §2.2. **No `uc_function` tools are attached directly.**
- **Why single-tool, deliberately:** an earlier revision also attached the same §2.3 UC functions directly to the Supervisor Agent, reasoning it could skip Genie when it already had exact `part_id`/`warehouse_id` keys (e.g. from the Lakeflow hand-off). In practice this meant the Supervisor called the old `needs_restock` / `restock_candidate_summary` functions straight from candidate JSON and **never invoked `genie_agent` at all** for a real Lakeflow-shaped prompt — defeating the point of having Genie do the deep analysis. Fixed by removing the direct `uc_function` tools; the Supervisor Agent's only path to any §4.2 logic is now a natural-language question to Genie. `scripts/ensure_supervisor_agent.py` actively deletes any non-`genie_agent` tool it finds, so this can't silently regress.
- **Why no DAB resource:** Supervisor Agents don't have a `resources:` entry type in Databricks Asset Bundles yet (as of this writing) — creation/tool-management is SDK-only. `scripts/create_supervisor_agent.py` is the "infrastructure as code" substitute: a re-runnable script that documents and reproduces the exact `create_supervisor_agent` + `create_tool` calls used, since there's no YAML to check in. `scripts/ensure_supervisor_agent.py` is the idempotent wrapper that reconciles an existing agent's description/instructions/tool set to match it.

### 2.5 Lakeflow Job → Supervisor Agent hand-off

`notebooks/lakeflow_trigger/invoke_supervisor.py` (task `invoke_supervisor`,
runs only when `has_candidates` is `true`):

1. Reads `candidates_json` from the `coarse_check` task value.
2. Builds a prompt embedding the candidate list and asks the Supervisor Agent to apply the veto, compute urgency + reorder qty, and produce one CRITICAL-first summary.
3. POSTs to `/serving-endpoints/{supervisor_endpoint_name}/invocations` using the OpenAI **Responses API** shape (`{"input": [{"role": "user", "content": ...}]}`) — this is the format Supervisor Agent endpoints expect, *not* the Chat Completions `{"messages": [...]}` shape `serving_endpoints.query()` builds by default, so this notebook calls `WorkspaceClient().api_client.do(...)` directly instead.
4. Extracts the final assistant message text from the response's `output` list (which interleaves `message` / `function_call` / `function_call_output` items) and sets it as a `supervisor_response` task value for any future downstream task (e.g. the not-yet-built `fact_restock_request` + `quote_metadata` write + Teams notification).
5. The `supervisor_endpoint_name` job parameter default (see `resources/jobs/lakeflow_trigger_job.yml`) is kept in sync automatically by `scripts/ensure_supervisor_agent.py` (called from `scripts/deploy_all.sh`), since endpoint names aren't stable across agent re-creations — never hardcode this value by hand.

**Timeout note:** cold-starting the Supervisor Agent, which then calls Genie
(itself an LLM call), which then calls one or more UC functions, took ~110s in
testing. The notebook passes a `databricks.sdk.config.Config(http_timeout_seconds=600,
retry_timeout_seconds=900)` **to the `WorkspaceClient(config=...)` constructor**
— `WorkspaceClient()` doesn't accept those as direct kwargs, and the SDK's
`ApiClient` reads them off `Config` once, at construction time, so setting
`w.config.retry_timeout_seconds = ...` *after* building the client silently
has no effect. Either mistake surfaces as `TimeoutError: Timed out after
0:05:00` (the SDK's default retry deadline) even though the call would have
succeeded given more time.

**Batch-size caveat (carried over from mock-data testing):** with a dozen
below-safety-stock candidates in one batch, the Supervisor Agent's
per-candidate orchestration (veto check + urgency/forecast calls, all routed
through Genie now that the Supervisor has no direct UC function tools) pushed
well past a workable timeout (>5 min, sometimes not completing at all) in
earlier testing against mock data. Now that
`fact_inventory_snapshot`/`fact_inventory_transaction`/`fact_procurement` are
Data Engineering's real, DE-managed dataset (seeded and scaled by them, not by
`notebooks/schema_bootstrap.ipynb`), re-verify this timeout behavior against
however many candidates the real `SAFETY_STOCK_QTY` check actually flags in
`gold_dev` — batching/pagination in `invoke_supervisor.py` may be needed if
the real candidate count per hourly run is large.

### 2.6 Restock Agent → not yet built

Still open. Architecture §4/§7 has it invoked only after human approval
(`fact_restock_request` row's `dim_request_status.DECISION = APPROVED`),
re-validating stock in real time and writing back `CONFIRMED_QTY` /
`FULFILLED_DATE_KEY` to `fact_restock_request`. Candidate approaches once the
Teams/Review-App HITL loop exists:

- Another Supervisor Agent tool, but per §2.4 that should mean routing the
  real-time re-check through Genie (a natural-language question), not
  attaching a `uc_function` tool for it directly to the Supervisor — keep the
  single-tool discipline; or
- A separate small Supervisor Agent invoked by the approval callback.

Not decided yet — revisit once the HITL surfaces (§5 of the architecture doc) are built.

---

## 3. What's deployed vs. what's still a stub

| Piece | Status |
|---|---|
| Lakeflow Job (`coarse_check` → `has_candidates` → `invoke_supervisor`) | Deployed via DAB, `PAUSED` |
| 7 UC functions (§2.3), reading Data Engineering's `gold_dev` star schema | Deployed via DAB (`deploy_uc_functions` job) |
| Genie Space, backed by `gold_dev` fact/dim tables only (not `quote_metadata`) | Deployed via DAB (`genie_spaces` resource) |
| Supervisor Agent + tools | Created via `scripts/create_supervisor_agent.py` (SDK, not DAB), reconciled to a single `genie_agent` tool via `scripts/ensure_supervisor_agent.py` (see §2.4 for why the direct UC function tools were removed) — re-verify the Lakeflow-shaped candidate-list prompt now routes through Genie instead of calling functions directly |
| `invoke_supervisor` → real Supervisor Agent call | Wired and tested against the live endpoint |
| `quote_metadata` table (companion to DE's `fact_restock_request`) | Deployed via DAB (`schema_bootstrap` job), seeded with 3 example rows |
| Teams Adaptive Card notification | Not built |
| Databricks Review App (Approve/Reject UI) | Not built |
| Restock Agent | Not built |

The Lakeflow Job ships **`PAUSED`** even though the full candidate → Supervisor
Agent → Genie/UC-function path is verified working, because there's no
Teams/Review-App loop yet downstream — unpausing now would just print the
Supervisor Agent's synthesized answer to job logs every hour with no human
ever seeing it. Unpause once the human-in-the-loop pieces above exist.
