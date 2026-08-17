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
| **Supervisor Agent** | A **Supervisor Agent** (Mosaic AI Agent Bricks, currently Beta/SDK-only) — an LLM-orchestrated router that owns a set of **tools**: sub-agents (like the Genie Space above) and/or UC functions directly. It decides *at runtime* which tool to call and in what order. | A hand-written orchestration graph (LangGraph-style) that we control step-by-step. |
| **"Deep analysis" logic** (§4.2: avg consumption, stockout forecast, urgency, quote text) | **Unity Catalog SQL functions** — governed, reusable, independently testable/queryable tools that *both* the Genie Space and the Supervisor Agent can call. | Python functions run inside a notebook task. |
| **Restock Agent** | Not yet built — will be either another Supervisor Agent tool (a UC function or a small agent) invoked after human approval. | — |

The key mental-model shift: **the deterministic work (§4.2 formulas) is not
"inside" any agent — it's UC functions that agents call as tools.** Genie and
the Supervisor Agent are both just different *front doors* onto the same six
UC functions; neither one re-implements the math.

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

- **Data sources:** `inventory_stock_level`, `threshold_config_table`, `consumption_history` (all in `ab_training.agentic_restock`).
- **Trusted UC functions:** all six functions from §2.3 below — this is how Genie gets access to "avg daily consumption" and "urgency" as first-class concepts instead of trying to reason about them from raw SQL.
- **Text instructions:** one consolidated instruction block telling Genie to prefer the UC functions over hand-rolled SQL for consumption/forecast/urgency questions, and to interpret "needs restocking" via the `current_stock_qty <= reorder_point_qty` rule from §4.1.
- **Example SQL:** a handful of `example_question_sqls` pairing natural-language questions with the exact SQL (including UC function calls) Genie should produce.
- **Resource:** `resources/genie/genie_agent.genie_space.yml` (DAB `genie_spaces` resource, requires `bundle: engine: direct` in `databricks.yml`) + `notebooks/genie/genie_agent.geniespace.json` (the serialized space config, round-tripped via `databricks bundle generate genie-space`).
- **Why DAB works here:** unlike Supervisor Agents (see below), Genie Spaces *do* have a first-class `genie_spaces` DAB resource type, so this one is fully IaC — `databricks bundle deploy` creates/updates it.

### 2.3 §4.2 deep-analysis logic → Unity Catalog SQL functions

Six UC functions in `ab_training.agentic_restock`, defined in
`notebooks/uc_functions/deep_analysis_functions.ipynb` and deployed via the
on-demand `resources/jobs/uc_functions_job.yml` (`deploy_uc_functions` job).

| Function | §4.2 formula it implements |
|---|---|
| `avg_daily_consumption(item_id, warehouse_id, lookback_days=14)` | Trailing-window average consumption |
| `predicted_stockout_date(item_id, warehouse_id)` | `today + current_stock_qty / avg_daily_consumption` |
| `classify_urgency(current_stock_qty, minimum_stock_qty, days_remaining)` | CRITICAL/HIGH/MEDIUM/LOW thresholds |
| `requested_restock_qty(item_id, warehouse_id)` | `target_stock_qty - current_stock_qty`, floored at 0 |
| `needs_restock(item_id, warehouse_id)` | The Genie Agent's "veto" power (§4, step 2) — **stub**, always `TRUE` until an inbound-shipment data source exists |
| `restock_candidate_summary(item_id, warehouse_id)` | Natural-language quote/assumption text for one candidate |

These are plain SQL (not Python UDFs) so they're queryable directly from a SQL
editor/notebook for debugging, and so both Genie and the Supervisor Agent can
call them without any serialization overhead. Two implementation quirks worth
knowing if you touch these:

- Internal calls between functions must be **fully qualified**
  (`ab_training.agentic_restock.avg_daily_consumption(...)`), not just the bare
  function name, or `CREATE FUNCTION` fails to resolve them.
- Any function body that does a **table scan + filter down to one row** (e.g.
  `WHERE item_id = X AND warehouse_id = Y`) must wrap the result in an
  aggregate (`MAX(...)`) — Spark's SQL function validator rejects correlated
  scalar subqueries that it can't prove return exactly one row, even when the
  filter is on a composite primary key.

### 2.4 Supervisor Agent → Supervisor Agent (Beta, SDK-only)

- **What it is:** `Agentic Restock - Supervisor Agent`, created via the Databricks SDK's `w.supervisor_agents` service (`name: supervisor-agents/9f6f2e6d-...`, serving endpoint `mas-9f6f2e6d-endpoint`).
- **Tools attached:**
  - `genie_agent` — a `genie_space` tool pointing at the Genie Space from §2.2, for open-ended natural-language questions across the three tables.
  - The same six UC functions from §2.3, attached directly as `uc_function` tools — so the Supervisor Agent can call e.g. `restock_candidate_summary` itself without going through Genie, when it already knows the exact `item_id`/`warehouse_id` (as it does when invoked from the Lakeflow Job with a candidate list).
- **Why no DAB resource:** Supervisor Agents don't have a `resources:` entry type in Databricks Asset Bundles yet (as of this writing) — creation/tool-management is SDK-only. `scripts/create_supervisor_agent.py` is the "infrastructure as code" substitute: a re-runnable script that documents and reproduces the exact `create_supervisor_agent` + `create_tool` calls used, since there's no YAML to check in.
- **Verified behavior:** querying the endpoint with "Which items need restocking right now, ordered by urgency? For the single most urgent one, explain why using the restock candidate summary tool" correctly: (1) called the `genie_agent` tool to get all CRITICAL/LOW candidates via natural language, (2) called `restock_candidate_summary` directly (bypassing Genie) for the single most urgent item, (3) synthesized both into one CRITICAL-first ranked answer. This confirms the Supervisor Agent is genuinely choosing between its two tool types at runtime, not just always deferring to Genie.

### 2.5 Lakeflow Job → Supervisor Agent hand-off

`notebooks/lakeflow_trigger/invoke_supervisor.py` (task `invoke_supervisor`,
runs only when `has_candidates` is `true`):

1. Reads `candidates_json` from the `coarse_check` task value.
2. Builds a prompt embedding the candidate list and asks the Supervisor Agent to apply the veto, compute urgency + reorder qty, and produce one CRITICAL-first summary.
3. POSTs to `/serving-endpoints/{supervisor_endpoint_name}/invocations` using the OpenAI **Responses API** shape (`{"input": [{"role": "user", "content": ...}]}`) — this is the format Supervisor Agent endpoints expect, *not* the Chat Completions `{"messages": [...]}` shape `serving_endpoints.query()` builds by default, so this notebook calls `WorkspaceClient().api_client.do(...)` directly instead.
4. Extracts the final assistant message text from the response's `output` list (which interleaves `message` / `function_call` / `function_call_output` items) and sets it as a `supervisor_response` task value for any future downstream task (e.g. the not-yet-built `open_request` write + Teams notification).
5. The `supervisor_endpoint_name` job parameter defaults to `mas-9f6f2e6d-endpoint` (see `resources/jobs/lakeflow_trigger_job.yml`) — override it if the Supervisor Agent is ever recreated (endpoint names aren't stable across `create_supervisor_agent.py` re-runs).

**Timeout note:** cold-starting the Supervisor Agent, which then calls Genie
(itself an LLM call), which then calls one or more UC functions, took ~110s in
testing. The notebook sets `http_timeout_seconds=600` / `retry_timeout_seconds=900`
on the `WorkspaceClient` accordingly.

### 2.6 Restock Agent → not yet built

Still open. Architecture §4/§7 has it invoked only after human approval
(`open_request.request_status = APPROVED`), re-validating stock in real time
and writing `restock_requests`. Candidate approaches once `open_request` and
the Teams/Review-App HITL loop exist:

- Another Supervisor Agent tool (UC function doing the real-time re-check + write), or
- A separate small Supervisor Agent invoked by the approval callback.

Not decided yet — revisit once the HITL surfaces (§5 of the architecture doc) are built.

---

## 3. What's deployed vs. what's still a stub

| Piece | Status |
|---|---|
| Lakeflow Job (`coarse_check` → `has_candidates` → `invoke_supervisor`) | Deployed via DAB, `PAUSED` |
| 6 UC functions (§2.3) | Deployed via DAB (`deploy_uc_functions` job) |
| Genie Space | Deployed via DAB (`genie_spaces` resource) |
| Supervisor Agent + tools | Created via `scripts/create_supervisor_agent.py` (SDK, not DAB) — verified working end-to-end |
| `invoke_supervisor` → real Supervisor Agent call | Wired and tested against the live endpoint |
| `open_request` table + write | Not built |
| Teams Adaptive Card notification | Not built |
| Databricks Review App (Approve/Reject UI) | Not built |
| Restock Agent | Not built |

The Lakeflow Job ships **`PAUSED`** even though the full candidate → Supervisor
Agent → Genie/UC-function path is verified working, because there's no
`open_request`/Teams/Review-App loop yet downstream — unpausing now would just
print the Supervisor Agent's synthesized answer to job logs every hour with no
human ever seeing it. Unpause once the human-in-the-loop pieces above exist.
