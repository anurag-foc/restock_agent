# Agent Bricks Mapping

How this pipeline maps onto real **Databricks Agent Bricks** primitives, what
was actually built, and why each piece looks the way it does rather than the
way a naive "one Python class per agent" reading would suggest.

This is the authoritative record of the *why*. Read it before touching
anything agent-related. For the current pipeline in plain language, see
[`end_to_end_walkthrough.md`](end_to_end_walkthrough.md); for the phase-1
product reasoning, [`market_evidence_phase1.md`](market_evidence_phase1.md).

> **Note on `docs/architecture.md`.** It describes the original single-layer
> design (§4.1 coarse check, §4.2 deep analysis) and is kept as historical
> record. Where this doc and the architecture doc disagree, this doc is
> current. Section references like "§4.2" below are to that doc's numbering
> and are retained only because the code comments still use them.

---

## 1. The core correction

Naming four "agents" (Lakeflow Job, Supervisor Agent, Genie Agent, Restock
Agent) suggests four peer components you'd each write as a Python class.
Agent Bricks doesn't work that way:

| Term | Is actually... | Not... |
|---|---|---|
| **Genie Agent** | A **Genie Space** — a natural-language interface over a fixed set of UC tables, configured with instructions, example SQL, and **trusted UC functions as tools**. It has no "code" of its own; all deterministic logic lives in the UC functions it's allowed to call. | A Python module with `avg_daily_consumption()` etc. as methods. |
| **Supervisor Agent** | A **Supervisor Agent** (Mosaic AI Agent Bricks, Beta/SDK-only) — an LLM-orchestrated router holding exactly three tools: two read-only Genie Spaces and one action MCP app. Every analysis question is forced through a Genie Space; every write goes through the MCP app. | A hand-written orchestration graph (LangGraph-style), or an agent with direct table/function access. |
| **"Deep analysis" logic** | **Unity Catalog SQL functions** — governed, independently queryable tools attached to the Genie Spaces *only*. | Python functions in a notebook task, or tools attached directly to the Supervisor. |
| **Restock Agent** | Not a separate agent. It's the **`restock_decision` job** (§2.6): a deterministic status write, then a fresh Supervisor turn using the `fulfillment_guardrail` Genie Space and the `fulfill_restock_request` action tool. | A fourth agent object. |

**The invariant, stated once:** *analysis is always reached through a
natural-language interface; writes are always reached through an idempotent
action tool.* The deterministic work is not "inside" any agent — it's UC
functions the Genie Spaces call. The Supervisor has no direct line to them and
only ever reaches them by asking a Genie Space a question.

---

## 2. Component-by-component mapping

### 2.1 Lakeflow Job → Databricks Job

A plain Databricks Job — cheap deterministic SQL, no Agent Bricks primitive
needed.

- **Resource:** `resources/jobs/lakeflow_trigger_job.yml` — `lakeflow_trigger`, **07:00 and 15:00 UTC**, ships `PAUSED`.
- **Tasks (two, linear — no condition task):**
  1. `refresh_signal_board` (`notebooks/signal_board/refresh_signal_board.py`) — runs `src/agentic_restock/jobs/signal_board.py`'s `CREATE OR REPLACE TABLE inventory_signal_board`: one row per (part, warehouse) over the full working set, every phase-1 nuance as a set-wise column.
  2. `invoke_supervisor` (`notebooks/lakeflow_trigger/invoke_supervisor.py`) — `COUNT(*)` over `rank_priority_actions(5)`; `dbutils.notebook.exit("NO_ACTION")` at zero, otherwise the three-turn conversation of §2.5.

**Why twice daily, not hourly.** [`market_evidence_phase1.md`](market_evidence_phase1.md) §3: an hourly cadence emitting every flagged part reproduces the alert fatigue this product exists to fix. The cadence is a product decision, not a performance tradeoff.

**Why the branching moved out of the job.** The old design had a `has_candidates` condition task between a `coarse_check` task and `invoke_supervisor`, with `coarse_check` passing `candidate_count` + `candidates_json` as task values. Both the condition task and the candidate list are gone:

- The **list** is gone on purpose. Handing the Supervisor pre-computed candidate JSON is exactly what made an earlier revision reason straight from that JSON and never call Genie (§2.4). The only reliable way to keep Genie on the critical path is for the job to genuinely not know the answer. A `COUNT(*)` is a boolean fact ("is there work"), not a judgment, so that much is safe.
- The **condition task** is gone because the count is now taken inside `invoke_supervisor` rather than the previous task, so an in-notebook exit is simpler than a task value plus a branch.

**Why `refresh_signal_board` doesn't report the candidate count** (it would be useful in the job UI): Spark validates a SQL function body at `CREATE` time, so the seven phase-1 functions can only be created once the board exists — which is why `deploy_uc_functions` runs the board refresh *before* `deploy_priority_functions`. If the board notebook called one of those functions, a fresh workspace would deadlock: the refresh fails on a missing function, so the function that would have fixed it never deploys. `invoke_supervisor` counts instead; it only runs once the functions exist.

**Also in the bundle:** `generate_sim_data` (`resources/jobs/simulation_data_job.yml`), an on-demand generator for ~18 months of simulated snapshots/transactions/POs plus a `sim_events` ground-truth table. Insert-only against DE's facts (explicit column lists, column verification, no `CREATE OR REPLACE`/`ALTER`, dimensions read-only) but it *does* delete and replace fact rows inside its generated date window, and `dry_run` defaults to `"false"`. Same `seed` reproduces the dataset byte for byte.

### 2.2 Genie Agent → Genie Space

Two Genie Spaces, both read-only, both DAB-native (`genie_spaces` resource type — requires `bundle: engine: direct`).

**`genie_agent` — "Manufacturing Inventory Intelligence Engine"** (`resources/genie/genie_agent.genie_space.yml` + `notebooks/genie/genie_agent.geniespace.json`, round-trippable via `databricks bundle generate genie-space`).

- **Data sources (14 tables):** `gold_dev.dim`'s `dim_part`, `dim_plant`, `dim_request_status`, `dim_supplier`, `dim_warehouse`; `gold_dev.supply_chain_analytics`' `dim_bom`, `dim_supplier_contract`, `fact_inventory_snapshot`, `fact_inventory_transaction`, `fact_plant_capacity`, `fact_procurement`, `fact_restock_request`, `inventory_signal_board`, `quote_metadata`.
- **`inventory_signal_board` is the important one.** It is Genie's primary read surface: the phase-1 functions are thin reads over it, so there is exactly one source of truth about a part/warehouse. If the board says a part has network surplus, `scan_transfer_options` cannot disagree.
- **`quote_metadata` is now included.** An earlier revision deliberately excluded it as "Teams/Review-App bookkeeping, not Genie's pre-quote analysis scope." That no longer holds: suppression and stalled-commitment detection need to see what has already been quoted and decided.
- **Trusted UC functions:** all **23** from §2.3. This is how Genie gets "seasonality-adjusted burn" or "decision value" as first-class concepts instead of reasoning about them from raw SQL.

**`fulfillment_guardrail` — "Fulfillment Guardrail"** (`resources/genie/fulfillment_guardrail.genie_space.yml`). A second, narrower space asked for exactly one verdict — `PROCEED` or `NEEDS_REVIEW` — after a PM approves a line. It catches the case where a request sat `PENDING_APPROVAL` long enough that the situation changed (replenished elsewhere, covered by a newer PO, demand collapsed). Read-only, and it deliberately **never proposes a quantity**: `fulfill_restock_request` computes `CONFIRMED_QTY`/`VARIANCE_QTY` itself from live data and only needs the verdict.

### 2.3 Deep-analysis logic → Unity Catalog SQL functions

**23 functions in `gold_dev.supply_chain_analytics`**, in two generations. Plain SQL, not Python UDFs, so they stay queryable from a SQL editor for debugging and Genie can call them with no serialization overhead. All are attached to the Genie Space **only** — never to the Supervisor (§2.4).

Deployed by the on-demand `deploy_uc_functions` job (`resources/jobs/uc_functions_job.yml`), three tasks, **order matters**: `deploy_functions` → `refresh_signal_board` → `deploy_priority_functions`.

**Generation 1 — 16 deep-analysis functions** (`notebooks/uc_functions/deep_analysis_functions.ipynb`), the Tier 1–4 intelligence set from `prd_v2.md`. Full per-function reference: [`uc_functions_reference.md`](uc_functions_reference.md).

`avg_daily_consumption` · `predicted_stockout_date` · `classify_urgency` · `dynamic_reorder_point` · `seasonality_adjusted_consumption` · `consumption_anomaly_score` · `requested_restock_qty` · `feasible_order_qty` · `pending_procurement_qty` · `supplier_reliability_score` · `ranked_suppliers` · `network_surplus` · `bom_component_requirements` · `assembly_risk_report` · `plant_capacity_check` · `financial_tradeoff_summary`

**Generation 2 — 7 phase-1 priority functions** (`notebooks/uc_functions/priority_functions.py` over `src/agentic_restock/jobs/priority_functions.py`), all thin reads over `inventory_signal_board`:

| Function | What it does |
|---|---|
| `rank_priority_actions(n)` | The ranking. `decision_value = GREATEST(exposure - action_cost, 0)`, where `action_cost` scales with the cheapest viable fix (≈3% of exposure for a transfer, 15–50% scaled by lead time for a buy, 100% if nothing helps). Also applies suppression. |
| `scan_transfer_options` | Donor warehouses and whether the donor still covers itself after the transfer |
| `scan_assembly_risk` | The threatened parent assembly and its value at risk |
| `scan_demand_shift` | Seasonality/trend movement in consumption |
| `scan_leadtime_drift` | Contracted vs. observed lead time |
| `evaluate_suppliers` | Reliability, lead time, cost ranking |
| `evaluate_feasibility(part, supplier, qty)` | MOQ / pack-size rounding — needs a chosen qty, so it can't be a board column |

**Two deliberate design positions, both easy to accidentally undo:**

- **No single `needs_restock` boolean.** An earlier revision had one, and it let the agent only echo `TRUE`/`FALSE` — never distinguishing "fully covered" from "partially covered" from "unconfirmable (no linked plant)". The veto is the reasoning this system exists to do. `rank_priority_actions` computes it as exposure vs. cost of the cheapest fix, with the formula intentionally simple and *visible* so it can be argued with, and Genie explains the result.
- **Suppression lives in `rank_priority_actions` as a `WHERE` clause**, not in Genie's instructions. An LLM that remembers to filter already-handled items 97% of the time re-raises a rejected item roughly monthly. Known simplification: `PENDING_APPROVAL`/`NEEDS_REVIEW`/`APPROVED`/`REJECTED` all suppress fully; re-surfacing on material change ("exposure grew 1.5× since the decision") needs an `EXPOSURE_AT_DECISION` captured at decision time, which `fact_restock_request` does not store.

**Redundant-but-retained.** Several generation-1 functions are now redundant with board columns (`classify_urgency`, `predicted_stockout_date`, `dynamic_reorder_point`, `consumption_anomaly_score`, …), and the three narrative-`STRING` ones (`assembly_risk_report`, `financial_tradeoff_summary`, `plant_capacity_check`) were a workaround for having no LLM in the loop — the board carries the numbers and Genie writes the sentence now. They are **not retired yet**, deliberately: `fulfillment_guardrail`'s space and the fulfillment path were never audited for dependence on them. Audit before deleting.

**Two Spark constraints that bite constantly:**

- Internal calls between functions must be **fully qualified** (`gold_dev.supply_chain_analytics.avg_daily_consumption(...)`), not the bare name, or `CREATE FUNCTION` fails to resolve them.
- A **scalar** function body that scans a table and filters to one row must wrap the result in an aggregate (`MAX(...)`). Spark's validator rejects correlated scalar subqueries it can't prove return exactly one row, even filtering on a composite primary key. Table-valued functions (`RETURNS TABLE (...)`) are exempt and can use plain `WHERE`/`QUALIFY ROW_NUMBER()`.
- Related: a scalar function whose body scans a fact table **cannot be called per row** at production volume (hundreds of thousands of part/warehouse pairs). That constraint is why the board computes every nuance set-wise as a column instead.

### 2.4 Supervisor Agent → Supervisor Agent (Beta, SDK-only)

- **What it is:** `Manufacturing Inventory Intelligence - Supervisor Agent`, created via the SDK's `w.supervisor_agents` service. Endpoint names change across re-creations — kept in sync automatically, see §2.5.
- **Tools attached: exactly three.**

| Tool id | `tool_type` | Points at | Access |
|---|---|---|---|
| `genie_agent` | `genie_space` | the intelligence space (§2.2) | read-only |
| `fulfillment_guardrail` | `genie_space` | the guardrail space (§2.2) | read-only |
| `inventory_intelligence_actions` | `app` | the `mcp-inventory-actions` app (§2.7) | the only writer |

  `scripts/ensure_supervisor_agent.py` reconciles this set and **deletes anything else it finds**.

- **Why no `uc_function` tools, ever.** An earlier revision attached the §2.3 functions to the Supervisor directly, reasoning it could skip Genie when it already had exact `part_id`/`warehouse_id` keys from the Lakeflow hand-off. In practice it called those functions straight from candidate JSON and **never invoked Genie at all** on a real prompt — defeating the design. Removing the tools fixed it; the reconciler makes it unable to regress silently. This is the single most important rule in the repo.
- **Why the action tools are a custom MCP app and not `uc_function`** (verified — do not re-litigate by trying again): a UC **SQL** function body containing `INSERT` fails with `PARSE_SYNTAX_ERROR`, because DML is not permitted in one. Hence a custom MCP server attached via the `app` tool type, which reaches it through **app authorization** rather than a UC HTTP Connection — no service principal or secret scope to configure on the Supervisor's side. The `app` tool type only accepts an `mcp-`- or `agent-`-prefixed app name, which is why the app must keep its `mcp-` prefix.
- **Why no DAB resource:** Supervisor Agents have no `resources:` entry type in Asset Bundles yet — creation and tool management are SDK-only. `scripts/create_supervisor_agent.py` is the as-code record of the description/instructions/tool prompts; `scripts/ensure_supervisor_agent.py` imports those constants and is the idempotent reconciler. **Run the reconciler in automation.** (Known wrinkle: `create_supervisor_agent.py` names its Genie tool `inventory_intelligence_engine` while the reconciler expects `genie_agent` and deletes everything else, so running the former then the latter recreates the tool under a different id.)

### 2.5 Lakeflow Job → Supervisor Agent hand-off

`notebooks/lakeflow_trigger/invoke_supervisor.py`. Several hard-won details live here; changing them tends to break things silently. `scripts/run_e2e_pipeline.py` mirrors all of it so the pipeline can be run from a laptop.

**Wire format.** The endpoint speaks the OpenAI **Responses API** shape (`{"input": [{"role", "content"}]}`), *not* Chat Completions. `serving_endpoints.query()` builds the wrong shape, so this calls `w.api_client.do("POST", "/serving-endpoints/{name}/invocations", ...)` directly. The reply text is buried in `response["output"][…]["content"][…]["output_text"]`, in an `output` list that interleaves `message` / `function_call` / `function_call_output` / `mcp_approval_request` items.

**Timeouts.** Pass them as `WorkspaceClient(config=Config(http_timeout_seconds=..., retry_timeout_seconds=...))`. `WorkspaceClient()` rejects them as direct kwargs, and the SDK's `ApiClient` reads them off `Config` once at construction — so setting `w.config.retry_timeout_seconds` afterwards is read too late and silently has no effect. Either mistake surfaces as `TimeoutError: Timed out after 0:05:00` (the SDK's default retry deadline) on a call that would have succeeded.

**The 290s ceiling and the three turns.** Model Serving cuts the HTTP connection at ~290s; the notebook sets a 280s per-turn timeout. Each request resets the clock, hence a fixed three-turn protocol:

1. **Turn 1 — rank.** One Genie call to `rank_priority_actions`; pick the top action by `decision_value` and echo its columns verbatim. Nothing else.
2. **Turn 2 — analyse.** One or two drill-down Genie calls (`scan_*`/`evaluate_*`) plus the board itself, then a resolution (transfer / PO / escalation with no action) written up in the OUTPUT CONTRACT format, stating the cost of acting *and* of doing nothing.
3. **Turn 3 — act.** `persist_quote`, then `send_human_review`.

Turns 1 and 2 were one round-trip first ("scan, analyse and decide") and reliably blew the ceiling — ranking plus drill-downs plus the write-up. That's the same collapse-into-one-prompt failure the old per-candidate loop was designed to avoid, re-triggered per turn instead of per candidate. Do not merge them back. (For the same reason, the old protocol's history-compression step and its `COMPRESSION_THRESHOLD` are gone: with one action per run there is no N-candidate history to compress.)

**The MCP approval round-trip.** Any custom-MCP (`app`-tool-type) call comes back as an `mcp_approval_request` item **instead of executing**. There is no way to disable this at tool-registration or per-request time — both were checked, neither is honored; Databricks requires an explicit approval round-trip. The endpoint is **stateless**, so `previous_response_id` chaining does *not* work: it returns `"Invalid message sequence. The approval response was in an unexpected position."` Continuing means resending the full transcript — everything sent so far, plus every item from the prior response's `output` **verbatim**, plus an `mcp_approval_response`. This is the documented way to consume the API, not a workaround, so the `_invoke` helper answers it inline within the same turn, bounded by `MAX_APPROVAL_ROUNDS = 5`. The helper is duplicated in `invoke_fulfillment.py` and `run_e2e_pipeline.py`; fix all three together.

**Verification, not retry.** Persistence and the Teams card are done by the Supervisor via the MCP tools. The tools are idempotent, so the notebook **never retries them** — it queries `quote_metadata` afterwards and raises if no row landed, warns if `teams_message_id` is unset. A silent no-write is the main failure mode of moving an action into an LLM's hands, so it's surfaced as a task failure rather than a log line nobody reads.

**Known contract gap.** `persist_quote`'s signature pre-dates this redesign and still expects `item_id, warehouse_id, current_stock_qty, reorder_point_qty, suggested_reorder_qty, initial_urgency` — not `rank_priority_actions`' output shape. Rather than a shim, Turn 3 tells the Supervisor to assemble `candidates_json` from the board. Updating the tool's contract natively is a real open follow-up.

**Endpoint name.** The `supervisor_endpoint_name` default in each job YAML is rewritten in place by `scripts/ensure_supervisor_agent.py` (from `deploy_all.sh`) across every file in its `JOB_YAMLS` list — currently `lakeflow_trigger_job.yml` and `restock_decision_job.yml`. **Never hardcode it**, and add new Supervisor-calling jobs to that list.

### 2.6 Restock Agent → the `restock_decision` job

Not a separate agent. `resources/jobs/restock_decision_job.yml`, triggered on demand by the review app's Final Submit:

1. `apply_decision` (`notebooks/restock_decision/apply_decision.py`) — deterministic, **no LLM**. Takes a batched `decisions_json` of `{restock_request_key, decision, note}` and applies each line's status change. `REQUEST_STATUS_KEY` is an FK into `dim_request_status`, which enumerates (status × urgency × decision) combinations, so the new key must be resolved against **each line's own current urgency** — otherwise every non-CRITICAL line silently gets relabelled CRITICAL. Sets `approved_keys_json` / `approved_count`.
2. `has_approval` — condition task, `approved_count > 0`.
3. `invoke_fulfillment` (`notebooks/restock_decision/invoke_fulfillment.py`) — **one fresh Supervisor turn per approved line**: ask `fulfillment_guardrail` for a `PROCEED`/`NEEDS_REVIEW` verdict, then call `fulfill_restock_request`, which computes `CONFIRMED_QTY`/`VARIANCE_QTY` itself and moves the line to `FULFILLING`.

**Why a job and not inline in the app:** the Databricks Apps reverse proxy hard-caps requests at 120s, and a cold Supervisor+Genie round-trip was measured at ~110s.

**Why no job-level `parameters:` block:** the app triggers this via AppKit's `jobs()` plugin, which for `taskType="notebook"` always sends legacy `notebook_params` — and the Jobs API rejects `notebook_params` on a job that has job-level parameters configured. Passing `decisions_json` through as a `notebook_params` override on `apply_decision`'s `base_parameters` sidesteps it. `supervisor_endpoint_name` isn't part of the app's call at all and stays a plain `base_parameters` default.

**The last hop is not agentic.** Marking a delivered line `COMPLETED` happens in the review app itself (`POST /api/lines/:lineKey/complete`), writing directly to `fact_restock_request`. There is no LLM step and no guardrail in recording that a delivery arrived, so a job would buy nothing but latency. It's idempotent (only acts on a line currently `FULFILLING`) and appends to `NOTE` rather than overwriting, so the approval-stage note survives.

### 2.7 Action tools → custom MCP app (`mcp-inventory-actions/`)

A Python Databricks App (FastMCP + FastAPI) built from Databricks' official "MCP Server - Hello World" template, deployed via `resources/apps/mcp_inventory_actions_app.yml`. Its own `README.md`/`Claude.md` are still the unmodified template and describe example health/user tools — **read `server/tools.py`, not those.**

Three tools, **each idempotent server-side by construction.** This is not defensive style: the Supervisor is an LLM and may retry or double-call, and a duplicate `fact_restock_request` row is a duplicate procurement order.

| Tool | Idempotency mechanism |
|---|---|
| `persist_quote(candidates_json, summary_report)` | Derives a deterministic `quote_id` from the candidate set + date |
| `send_human_review(quote_id, summary_report, force_resend=False)` | No-ops if `teams_message_id` is already set (unless forced) |
| `fulfill_restock_request(restock_request_key, proceed, note)` | Only acts on a line currently `APPROVED` |

Runs SQL through the app's own service-principal-authenticated `WorkspaceClient` (`server/db.py`, `server/utils.py::get_workspace_client`) against `DATABRICKS_WAREHOUSE_ID` — not on-behalf-of-user auth, since the caller is the Supervisor, not an interactive user.

#### Required Unity Catalog grants (one-time, after first deploy)

App authorization to the Supervisor succeeds regardless of UC permissions, so a
missing grant surfaces as a **silent SQL failure inside a tool call**, not an
auth error. `deploy_all.sh` prints the resolved service principal id as a
reminder but does not apply these.

```sql
-- <sp> = the app's auto-provisioned service principal
--        (databricks apps get mcp-inventory-actions -o json)
GRANT USE CATALOG ON CATALOG gold_dev                                TO `<sp>`;
GRANT USE SCHEMA  ON SCHEMA  gold_dev.dim                            TO `<sp>`;
GRANT USE SCHEMA  ON SCHEMA  gold_dev.supply_chain_analytics         TO `<sp>`;

-- read: dim_part, dim_warehouse, dim_request_status, fact_inventory_snapshot
GRANT SELECT ON SCHEMA gold_dev.dim                                  TO `<sp>`;
GRANT SELECT ON SCHEMA gold_dev.supply_chain_analytics               TO `<sp>`;

-- write: the only two tables it writes
GRANT INSERT, UPDATE ON TABLE gold_dev.supply_chain_analytics.fact_restock_request TO `<sp>`;
GRANT INSERT, UPDATE ON TABLE gold_dev.supply_chain_analytics.quote_metadata       TO `<sp>`;
```

Plus `CAN_USE` on the SQL warehouse (declared as an app resource in the YAML)
and `READ` on the `restock-agent/teams-webhook-url` secret (likewise).

---

## 3. What's deployed

| Piece | Status |
|---|---|
| Lakeflow Job (`refresh_signal_board` → `invoke_supervisor`) | Deployed via DAB, **`PAUSED`** |
| `inventory_signal_board` + `scan_run_log` | Built on demand by the job / `deploy_uc_functions` |
| 23 UC functions (16 deep-analysis + 7 phase-1) | Deployed via DAB (`deploy_uc_functions`, 3 tasks) |
| Genie Space `genie_agent` (14 tables, 23 trusted functions) | Deployed via DAB (`genie_spaces` resource) |
| Genie Space `fulfillment_guardrail` | Deployed via DAB |
| Supervisor Agent + its 3 tools | SDK, not DAB — `scripts/ensure_supervisor_agent.py` |
| `mcp-inventory-actions` app (3 action tools) | Deployed via DAB; **UC grants are manual** (§2.7) |
| `quote_metadata` | Deployed via DAB (`schema_bootstrap`) |
| Teams Adaptive Card notification | Live, via `send_human_review` |
| `restock-review` app (per-line approve/reject, batched submit, fulfilling orders) | Deployed via DAB |
| `restock_decision` job (apply → guardrail → fulfill) | Deployed via DAB |
| `generate_sim_data` job + `sim_events` ground truth | Deployed via DAB, on demand |
| MLflow evaluation / monitoring | **Not built** |

**Why the Lakeflow Job still ships `PAUSED`.** The full path is verified
end to end, so this is no longer "the downstream doesn't exist." It's that
unpausing starts writing real quotes and notifying real people on a schedule,
and the two open validation items in
[`market_evidence_phase1.md`](market_evidence_phase1.md) §16 are unresolved:
whether `decision_value` ranking actually differs from raw exposure ordering
on real data, and whether the cost weights inside it are right. Unpause once
those are checked against real outcomes.
