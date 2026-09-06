# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Inventory Intelligence" / "Manufacturing Inventory Intelligence Engine" — a Databricks **Agent Bricks**
pipeline that scans manufacturing inventory twice a day, detects up to eight distinct kinds of problem
at each one's own natural grain (stockout risk, BOM cascade blocks, lateral-transfer opportunities, dead
capital, lead-time drift, demand shift, supplier economics, MOQ-uneconomic orders), prices each one in
real rupees, picks the highest-decision-value handful under a hard output budget, and notifies a human
in Microsoft Teams for approval.

The "a handful of actions, twice a day, bundled into one notification" shape is deliberate and is the
product thesis, not a limitation: an hourly cadence emitting every flagged part reproduces the alert
fatigue this product exists to fix. One real MRP run produced 8,366 action messages in a week — a
bounded, ≤4-line, single-notification quote is the antidote, not a departure from ambition.

Deployment is **exclusively** via Databricks Asset Bundles (`databricks.yml` + `resources/**/*.yml`). No
manual notebook uploads, no click-ops job creation — except the Supervisor Agent, which has no DAB
resource type yet (see below).

### This pipeline was rebuilt once already — read this before assuming a design choice is arbitrary

The system used to have a structural defect: it claimed eight intelligence "nuances" but could
structurally surface only two, because the old ranking's `signal_type` was computed by one
mutually-exclusive `CASE` expression over a (part, warehouse) row. The other six nuances — including
lateral transfer, the strongest single opportunity in the product — were flattened into board columns
that could never be *the reason* a candidate was raised.

The fix was a full redesign of the detection layer, built and gated in six phases against a purpose-
built dataset: **[docs/redesign_tracker.md](docs/redesign_tracker.md)** is the complete record of that
work — every phase, every measured gate, every bug that produced plausible-but-wrong output rather than
failing outright, and the two live fabrication incidents that shaped the current design. Read it before
assuming any of the choices below (the exposure formula, the output budget, the one-tool Supervisor) are
arbitrary — most of them are a direct response to something that went wrong the other way first.

The design doc it worked from: **[docs/intelligence_layer_design.md](docs/intelligence_layer_design.md)**
(detection at each nuance's own grain; exposure as `P(stockout) × consequence` rather than
`shortfall × unit_cost`; `action_cost` as a real rupee figure from real fix construction). The dataset
it's measured against: **[docs/dataset_generator_spec.md](docs/dataset_generator_spec.md)**. Schema
additions this project needed on top of Data Engineering's real schema:
**[docs/schema_changes_gold_dev_analytics.md](docs/schema_changes_gold_dev_analytics.md)**.

**The old pipeline has been removed, not kept side by side.** It ran on `gold_dev` while this one was
built and gated against a replica catalog (`gold_dev_analytics`), so the two could be diffed without
touching production data — but once the redesign's six phases all passed, the old signal board, its 8
UC functions, both Genie Spaces, the 2+N-turn Supervisor protocol, and the Supervisor-mediated
fulfillment turn were deleted. There is one pipeline now. `docs/redesign_tracker.md`'s own "Retirement"
section is the checklist that removal followed. **[docs/end_to_end_walkthrough.md](docs/end_to_end_walkthrough.md)**
was rewritten for the current pipeline and is the plain-language entry point — read it first. A few
docs elsewhere in `docs/` (`agent_bricks_mapping.md`, `market_evidence_phase1.md`,
`uc_functions_reference.md`) still describe the old architecture in detail and have **not** been
rewritten; treat them as historical unless a specific claim has been re-verified against the code.

A production catalog cutover (`gold_dev_analytics` → `gold_dev`) has **not** happened — that is a
separate, real decision, not implied by the retirement above. `AGENTIC_RESTOCK_GOLD_CATALOG` still
defaults through `gold_dev`, but every job resource in this bundle currently overrides it to
`gold_dev_analytics` explicitly.

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

Scripts that hit the live workspace are run with `PYTHONPATH=src python3 scripts/<x>.py` (they are not pytest tests): `add_restock_note_column.py` (one-off idempotent `ALTER TABLE` adding `fact_restock_request.NOTE`, which holds the PM's free-text approve/reject reasoning), `generate_analytics_dataset.py` (the dataset generator for `gold_dev_analytics` — `--report` prints coverage, `--dry-run` runs the assertion gate and writes nothing, catalog-guarded so it refuses to load if assertions fail), `seed_demo_scenarios.py` (a separate, older demo-data feeder that idempotently seeds `dim_bom`/`dim_supplier_contract`/`fact_supplier_delivery` on **`gold_dev`** specifically — kept for `gold_dev` demo maintenance per `docs/redesign_tracker.md`'s Retirement section, independent of which detection code reads that catalog; `--prune-quotes` is the demo reset for the approval queue, dropping every quote except `PRESERVED_QUOTE_IDS` and the `--keep-recent N` most recent line-bearing quotes; `--rebuild-facts` is DESTRUCTIVE and replaces `fact_inventory_snapshot`/`fact_inventory_transaction`/`fact_procurement` wholesale — see the script's module docstring for what it preserves).

## Architecture — the invariant that governs everything

**Analysis is deterministic Python, computed and attached to every finding as evidence before the
Supervisor ever sees it; writes are always reached through an idempotent action tool.** The detectors
(pure Python over pandas/numpy, Spark only at the read/write edges) compute exposure, price the fix, and
build the full arithmetic trail for every finding — there is nothing left for an LLM to look up or
compute. Nothing is a Python "agent class"; the only agentic step is one Supervisor turn that writes
prose around numbers it did not produce, then calls the write tools.

```
Lakeflow Job "intelligence" (07:00 + 15:00 UTC, PAUSED)
  refresh_positions  → builds part_position / parent_cascade / supplier_performance from
                       3 years of history: corrected burn rate (Croston + seasonal), lead-time
                       distribution (μ/σ/drift/P90), cascade rollup (value counted once, at the
                       parent). Measurement only -- no exposure, no ranking, no fix live here.
  run_intelligence   → 8 scanners (S1-S8), each at its own natural grain, find every problem →
                       suppress findings with a live decision against them (reads
                       fact_restock_request) → collapse duplicates (a cascade subsumes its
                       binding children; a transfer and a purchase on the same pair are
                       alternatives, not two problems) → apply a hard output budget (default 4)
                       with a diversity constraint (max 2 findings per type) → build ONE brief
                       with every figure and arithmetic trail pre-formed → ONE Supervisor turn:
                       write the WHY NOW prose, call persist_quote then send_human_review

restock-review app (Databricks App)
  PM stages approve/reject + a reason code + a note per line, then one Final Submit
    → triggers the restock_decision job with a batched decisions_json
    apply_decision     → deterministic status write for every line, no LLM. APPROVED goes
                         STRAIGHT to FULFILLING at the proposed quantity (no separate machine
                         step in between any more), plus a non-blocking advisory check (an open
                         PO may already cover it) via one UC SQL function called directly.
  Later, on the Fulfilling Orders page, the PM marks a line delivered
    → POST /api/lines/:lineKey/complete writes COMPLETED directly (see below)

Supervisor Agent (SDK-only, Beta) — exactly ONE tool:
  inventory_intelligence_actions  app → custom MCP server, mcp-inventory-actions app
                                        (persist_quote, send_human_review)
```

**The Supervisor's tool set is exactly that one tool**, reconciled by `scripts/ensure_supervisor_agent.py`,
which deletes anything else it finds. There used to be three: `genie_agent` (a Genie Space over the old
signal board's 8 UC functions) and `fulfillment_guardrail` (a Genie Space that re-checked an approved
line before the old fulfillment turn) have both been removed — see `docs/redesign_tracker.md`'s
Retirement section for exactly what each one did and why removing it was safe. The rule this still
enforces is the same one it always did: **never attach UC functions to the Supervisor directly, and
never let it reach an analysis surface it can bypass the detectors through.** With the detectors
computing every figure up front, there is genuinely nothing left to consult — the brief is the only
source of fact the model has, which is the strongest form of that guarantee.

**Why the action tools are a custom MCP app and not `uc_function`** (verified, do not re-litigate by
trying again):
- A UC **SQL** function body containing `INSERT` fails with `PARSE_SYNTAX_ERROR` — DML is not permitted.
- Hence: a custom MCP server (`mcp-inventory-actions/`, built from Databricks' official "MCP Server - Hello World" app template), attached to the Supervisor directly via the `app` tool type. `app` tool type only accepts an `mcp-`- or `agent-`-prefixed app name, and reaches it via **app authorization** rather than a UC HTTP Connection — no service principal/secret scope to set up for the Supervisor's side. The app's own auto-provisioned service principal still needs Unity Catalog grants on the tables it writes (see `docs/agent_bricks_mapping.md`, though that doc predates the redesign — the catalog is now `gold_dev_analytics`, not `gold_dev`) — a one-time `GRANT`, not an OAuth/secret-scope dance.

**Every action tool enforces its own idempotency server-side** (`mcp-inventory-actions/server/tools.py`)
— the Supervisor is an LLM and may retry or double-call, and a duplicate `fact_restock_request` row is a
duplicate procurement order. `persist_quote` derives a deterministic `quote_id` from the candidate set +
date; `send_human_review` no-ops if `teams_message_id` is set. The notebook *verifies* the tools ran and
fails loudly if not — it never retries them.

Read [docs/agent_bricks_mapping.md](docs/agent_bricks_mapping.md) for background on why the agent wiring
looks the way it does, but note it **predates the redesign** and still describes the removed Genie
Spaces and the removed `fulfill_restock_request` tool as current — cross-check anything specific against
`docs/redesign_tracker.md` and the code before relying on it.

### The corrected-measures layer (`src/agentic_restock/jobs/positions.py`, `estimators/`)

Replaces the old signal board. Where the board mixed measurement with judgement in one wide row per
(part, warehouse) — burn, lead time, transfer options, cascade exposure and a ranking hint all in one
place — this layer holds **only corrected measurement**: what is on hand, how fast it is really moving,
how long the supplier really takes, and how well each of those is known. No exposure, no ranking, no fix
— those are the scanners' job, and keeping them out is what lets a scanner run at its own natural grain
instead of being flattened into one row.

Three tables, rebuilt each run by `notebooks/positions/refresh_positions.py` (the only Spark-aware part;
everything else is pure functions over pandas/numpy DataFrames, testable without a cluster):

- **`part_position`** — one row per (part, warehouse). `estimators/burn.py` (E1) estimates forward burn:
  intermittency (≥70% zero days) is tested first and routed to Croston, since a daily mean on a lumpy
  series describes neither the lumps nor the gaps; otherwise a deseasonalised level is re-seasonalised
  for the replenishment horizon that matters, with a staleness branch (keyed to multiples of the part's
  own demand interval, not a fixed day count) so a genuinely dead part isn't reported with a healthy
  burn rate. `estimators/leadtime.py` (E2) estimates the lead-time distribution (μ/σ/drift/P90) with
  hierarchical fallback tiers (pair → supplier → category) when a specific (part, supplier) pair has too
  few observations.
- **`parent_cascade`** — one row per (assembly, plant store), built by rolling demand DOWN from the
  production plan (model → assembly → component) and constraining it by what each descendant can
  actually supply. Value is counted **once**, at the parent — the superseded design let every blocking
  child claim the parent's full value, which inflated a single cascade by however many children happened
  to bind. Restricted to `PLANT_STORE` warehouses; assessed everywhere, a first pass found 51 "blocked"
  parents worth ₹618 cr almost entirely at distribution centres that never build anything.
- **`supplier_performance`** — one row per (part, supplier): E2's lead-time estimate plus observed
  reject rate and mean freight cost, used by the sourcing and lead-time-signal scanners.

`estimators/risk.py` (R) then attaches `P(stockout) × consequence` to every `part_position` row — the
central change from the superseded `shortfall_qty × unit_cost` formula, which can't express likelihood
at all and ranks a near-certain small loss below a remote large one. Consequence prefers a real cascade
value when one exists; otherwise it falls back to a criticality-weighted multiple of unserved demand
(flagged in `assumptions_used` as an estimate, never presented as measured — this is documented as **the
weakest number in the system**, see `docs/intelligence_layer_design.md` §6.1).

### The detectors (`src/agentic_restock/detectors/`)

**`scanners.py`** — S1 through S8, one function per finding type, each running at its own natural grain
rather than being flattened onto a (part, warehouse) row:

| | finding type | grain | what it finds |
|---|---|---|---|
| S1 | `STOCKOUT_RISK` | (part, warehouse) | will run out before a replenishment can land — tested as `days_of_cover < effective_lead + review_period`, not `on_hand < safety_stock` (the latter is the ERP's own threshold test and produces thousands of messages a week without saying whether it will actually hurt) |
| S2 | `CASCADE_BLOCK` | (parent, warehouse) | names the **whole binding set** — "buy X and unblock" is false if Y binds at the same level |
| S3 | `REDEPLOYMENT` | part, across the network | stock worth more somewhere else than where it is — a matching problem, not a threshold. Structurally couldn't exist as a finding type before this redesign; the evidence base calls it the strongest single opportunity in the product |
| S4 | `DEAD_CAPITAL` | (part, warehouse) | stock that will never be consumed — the excess half of the network view no incumbent surfaces; exposure is the *annual carrying cost*, not the stock value, since the capital is idle, not lost |
| S5 | `LEADTIME_SIGNAL` | supplier | reported **once per supplier**, not once per part it affects — fires on drift OR spread, since a supplier exactly on contract with a wide spread is unmanageable and a mean-only test sees nothing wrong with it |
| S6 | `DEMAND_SHIFT` | (part, warehouse) | corrected burn materially disagrees with the snapshot's flat average, and the safety stock was calibrated for the old rate |
| S7 | `SUPPLIER_ECONOMICS` | (supplier, part) | the cheapest quote is not the cheapest supplier once rejects and lead-time spread are priced in |
| S8 | `MOQ_UNECONOMIC` | (part, supplier) | the minimum order forces an overbuy that costs more than the risk it removes — the answer is renegotiating the pack, not placing the order, which is why it's its own action type rather than a footnote on a buy |

**`fixes.py`** (FX1–FX3) prices every fix as a **real rupee figure**, never a percentage of exposure —
the superseded ranking used `exposure × 0.03/0.15–0.50/1.00` for transfer/buy/nothing, a plausible-looking
number available for every candidate before any option was chosen, which is exactly why the model kept
presenting it to PMs as a price (see the fabrication history in `docs/redesign_tracker.md`). FX1 (transfer)
ranks donors by **cover given up**, not quantity held, and prices the benefit as net risk removed at the
receiver minus risk created at the donor. FX2 (supplier) turns "unreliable" into rupees via a variance
premium. FX3 (purchase quantity) is **computed, never chosen** by the caller — letting a caller supply
the quantity is what silently disabled the MOQ constraint in the old design.

**`findings.py`** is the common shape all eight scanners emit: `exposure`, `exposure_basis` (the
derivation in words and figures — there is no single formula, so this has to travel with the number),
`action_cost`, `decision_value` (`exposure - action_cost`, floored at zero), `evidence` (a dict populated
at detection time, dumped verbatim by the narration layer — this is what removes drill-down turns from
the pipeline, not by optimising them but by leaving nothing for one to fetch), and `assumptions_used`
(only the policy inputs *this* finding's numbers actually depend on — a transfer discloses no holding
rate, because it spends nothing and holds nothing extra).

**`selection.py`** does two jobs, and the second matters more than it looks:

- **Suppression** (nuance 8) drops a finding while a decision on it is still live, keyed on each scanner's
  own `suppression_key` — a supplier-grain finding suppresses on a supplier decision, not a decision about
  one of its parts, which the old (part, warehouse)-only suppression couldn't express. A commitment
  suppresses only while fresh: `PENDING_APPROVAL`/`NEEDS_REVIEW` re-surface after 2 days, `APPROVED`/
  `FULFILLING` after `lead_days + 3`. A `REJECTED` decision stays suppressed **unless** its structured
  `DECISION_REASON` says otherwise: `CANNOT_ACT_NOW` is a snooze (returns after 14 days on its own, since
  the constraint that blocked it — a budget freeze, someone on leave — lifts without telling this system),
  while `NOT_A_PROBLEM`/`NUMBERS_WRONG` close the finding until its exposure grows 1.5× what it was worth
  at decision time. The distinction exists because "not a real problem" and "can't act right now" read
  almost identically in free text but are opposite signals — collapsing them teaches the system to stop
  surfacing hard-but-important findings, which is alert fatigue inverted and much harder to notice.
- **Selection** enforces the hard output budget (default 4) with a round-robin diversity constraint (max
  2 per finding type) rather than a global top-N — a global top-N spends the whole budget on whichever
  scanner produces the largest numbers, which is guaranteed to be the same scanner every run since
  exposure varies by orders of magnitude between types. Measured: ranking by decision value instead of
  raw exposure reorders the *middle* of the list substantially but leaves the top 3–5 identical (at the
  top, exposure exceeds any fix cost by two to three orders of magnitude, so subtracting cost can't
  reorder anything) — so at a budget of 3–4, **the diversity constraint is what changes what a PM sees,
  not the ranking formula.**

### The brief and the single Supervisor turn (`src/agentic_restock/narration.py`, `jobs/run_intelligence.py`)

`narration.build_brief()` assembles the one message the Supervisor turn is built from. It carries three
things, and the third is the actual design decision:

1. The evidence, as a verbatim field dump (no editorial latitude — a summary has room to fabricate, a
   dump does not).
2. The output skeleton (`## ACTION ITEM i of N`, `RECOMMENDATION`, `OPTIONS CONSIDERED`, `EVIDENCE`,
   `IF APPROVED AND WRONG`, `DECISION VALUE`, `ASSUMPTIONS USED`), so the format lives in code, not in
   agent instructions where it competes with everything else for attention.
3. **The arithmetic, already performed** — not "state the holding cost" but the full computation with
   the total last, pre-formatted. Every one of the fabrication incidents recorded in
   `docs/redesign_tracker.md` had a prose rule forbidding it *at the time it happened*; what stopped them
   for good was never a better rule, it was removing the slot the wrong number could go into.

`persist_quote`'s exact arguments are pre-assembled too (`narration.quote_lines()`), already resolved to
`PART_ID`s — a live run once wrote a full report with **zero** part-lines because the model reached for a
part *name* instead. `jobs/run_intelligence.py` posts one turn to the Supervisor endpoint (Responses API,
same platform constraints as below), answers any `mcp_approval_request` inline, and verifies
`persist_quote`/`send_human_review` were both actually called before declaring success.

**Constraints that don't change from the old design, because they're platform constraints, not protocol
choices:**
- The endpoint speaks the OpenAI **Responses API** shape (`{"input": [...]}`), not Chat Completions —
  `serving_endpoints.query()` builds the wrong shape, so this posts to
  `/serving-endpoints/{name}/invocations` directly. Reply text is at
  `response["output"][…]["content"][…]["output_text"]`.
- Timeouts must be passed as `WorkspaceClient(config=Config(http_timeout_seconds=..., retry_timeout_seconds=...))`.
  Setting `w.config.*` after construction is read too late and silently has no effect — the symptom is
  `TimeoutError: Timed out after 0:05:00`.
- **Custom MCP tool calls require an explicit approval round-trip.** Any `app`-tool-type call comes back
  as an `mcp_approval_request` item instead of executing, with no way to disable that at registration or
  per-request time. The endpoint is **stateless**, so continuing means resending the whole transcript —
  everything sent so far, plus every item from the prior response's `output` **verbatim**, plus an
  `mcp_approval_response`.

**What genuinely changed:** the old design needed 2+N Supervisor turns (a pre-check, a scan turn, one
analysis round-trip per candidate, a final persist/notify turn) because ranking plus per-candidate
Genie drill-downs plus the write-up reliably exceeded Model Serving's ~290s HTTP gateway ceiling. The
redesign removes the reason for the split rather than optimising it: every figure is computed by the
detectors before the brief is built, so there are no drill-downs left to make. What remains is a
write-up plus two tool calls, which fits comfortably inside one turn.

### Scan run log (`src/agentic_restock/jobs/run_log.py`)

One `scan_run_log` row per scan run, written whether or not a Supervisor turn happened (`NO_ACTION` vs
`SUPERVISOR_INVOKED`). Without it, "nothing needed attention" and "the job silently broke" are
indistinguishable from outside, and the alert-fatigue counterpoint this product leans on ("quiet on 8 of
14 runs this week") has no evidence behind it.

### Review App (`restock-review/`)

An AppKit (Node/React) Databricks App, deployed as part of the same bundle
(`resources/apps/restock_review_app.yml`, `source_code_path: ../../restock-review`). Pages:
`PendingQuotesPage`, `QuoteDetailPage` (plus `IntelligenceReport.tsx`, which parses the Supervisor's
`summary_report` text into the OUTPUT CONTRACT sections the detail page renders, colour-coded by
`action_type` and citing each figure back to its `evidence` field), `FulfillingOrdersPage`.

A PM decides **per part-line** — `fact_restock_request`'s grain is one row per part-line — but decisions
are **staged in the UI and submitted as one batch**: `POST /api/quotes/:quoteId/decisions` takes
`{lineKey, decision, note, reason}[]` (a rejection requires a structured `reason` code — see
`selection.py` above for what each one changes about the next run), pre-validates, and triggers the
`restock_decision` job once with a batched `decisions_json`. It does not write.

**One endpoint does write, deliberately:** `POST /api/lines/:lineKey/complete` flips a line
`FULFILLING → COMPLETED` (and appends to `NOTE`) straight from the app. There is no LLM step and no
guardrail in marking a delivery received, so routing it through a job would buy nothing but latency. It
is idempotent — it only acts on a line currently `FULFILLING`, and appends to `NOTE` rather than
overwriting so the approval-stage note survives. Everything the *agent* does still goes through the MCP
action tools; this is a human's deterministic status flip, and it is the only other write in the app.

Analytics caching is explicitly **disabled** (`cache: { enabled: false }` in `server/server.ts`). The
default shared cache served stale rows after a decision was written, which on an approval screen means
showing a PM that a line they just approved is still pending.

Local dev: `npm run dev` (port 8000). `useAnalyticsQuery` has no `refetch()`, so the UI forces a refresh
by remounting via a changing `key`. Analytics query params must be wrapped (`sql.string(...)`) — the wire
format is `{"__sql_type":"STRING","value":"..."}`, and a bare string is rejected server-side.

### Action MCP server (`mcp-inventory-actions/`)

A Python Databricks App built from Databricks' official "MCP Server - Hello World" template (FastMCP +
FastAPI), deployed via `resources/apps/mcp_inventory_actions_app.yml`. Exposes `persist_quote`,
`send_human_review` as MCP tools (`server/tools.py`), each idempotent by construction (see above). Runs
SQL via the app's own service-principal-authenticated `WorkspaceClient` (`server/db.py`,
`server/utils.py::get_workspace_client`) against `DATABRICKS_WAREHOUSE_ID` — not on-behalf-of-user auth,
since the caller is the Supervisor Agent, not an interactive user. The app's service principal needs
`USE CATALOG`/`USE SCHEMA`/`SELECT` on `gold_dev_analytics.dim` and `gold_dev_analytics.supply_chain_analytics`,
plus `INSERT, UPDATE` on `fact_restock_request` and `quote_metadata`, plus `CAN_USE` on the SQL warehouse
— grant these once after first deploy; a missing grant surfaces as a silent SQL failure inside a tool
call, not an auth error, since app authorization to the Supervisor succeeds regardless.

**`persist_quote` resolves surrogate keys up front and counts rows, not loop iterations**, for the same
reason described in `docs/redesign_tracker.md`: an unresolved id raises with a message naming it, and
idempotency is judged on the lines so a partial write repairs itself. It resolves `PART_KEY`,
`WAREHOUSE_KEY`, `SUPPLIER_KEY` and a status key up front, and writes `ACTION_TYPE`, `FINDING_TYPE`,
`SUBJECT_KEY`, `SOURCE_WAREHOUSE_KEY`, `RECOMMENDED_SUPPLIER_KEY`, `EXPOSURE_AT_DECISION` alongside the
purchase-shaped columns `fact_restock_request` originally had — because six of the eight finding types
don't fit "part, warehouse, supplier, quantity" (a transfer has a donor warehouse and no supplier; a
lead-time signal has no part at all). A candidate with no `item_id` must carry a `subject_key` instead
(e.g. `supplier:SUP010`), or the line can't be identified or suppressed later.

**The Teams card is deliberately a nudge, not the report.** `_build_adaptive_card` parses `summary_report`
into its `## ACTION ITEM i of N` blocks (accepting the older `## CANDIDATE` marker too, for quotes written
before this format) and emits a `Found N action item(s)` header plus one bold line per item (the
`RECOMMENDATION`) with a subtle second line (`Rs <exposure> at stake · <first sentence of WHY NOW>`), then
a plain `Review in Databricks` button. Options considered, evidence, and the if-approved-and-wrong
arithmetic all live in the Review App, which is where a decision is actually made.

### UC SQL functions

**16 legacy functions in `gold_dev.supply_chain_analytics.deep_analysis_functions.ipynb`** (the old
§4.2 deep-analysis tier) remain deployed, but only **one is still called by anything**:
`pending_procurement_qty`, invoked directly by SQL inside `apply_decision.py`'s advisory check — no
Genie Space in front of it. The other 15 stay deployed for ad-hoc SQL-editor debugging, unattached to any
Genie Space or Supervisor tool. `resources/jobs/uc_functions_job.yml`'s `deploy_uc_functions` job has one
task now (`deploy_functions`, `CREATE OR REPLACE`, idempotent) — the tasks that used to rebuild the old
signal board and deploy its 8 phase-1 functions are gone along with the source files that built them.

There is deliberately **no single `needs_restock` boolean**. The restock veto is the reasoning this
system exists to do — the detectors compute `P(stockout) × consequence` against the cost of the cheapest
viable fix, and the Supervisor writes the sentence around the result.

## Data layer

`src/agentic_restock/config.py` is the single source of truth for catalog/schema/table names, overridable by `AGENTIC_RESTOCK_{GOLD_CATALOG,DIM_SCHEMA,FACTS_SCHEMA,CATALOG,SCHEMA}`. Import from it rather than hardcoding names.

Every job resource in this bundle currently points `gold_catalog`/`app_catalog` at `gold_dev_analytics` —
a replica catalog this project owns, populated by `scripts/generate_analytics_dataset.py` and kept
alongside Data Engineering's real `gold_dev` so the redesign could be built and measured without
touching production data. `CATALOG`/`SCHEMA` (our artifacts) and `GOLD_CATALOG`/`FACTS_SCHEMA` (DE's
facts) stay separate config knobs because the *ownership* distinction is real regardless of which
physical catalog either currently points at.

**Data Engineering's, read-only** — `fact_inventory_snapshot`, `fact_inventory_transaction` (`ISSUE`
rows = consumption), `fact_procurement`, `fact_supplier_delivery`, `fact_supplier_quality`, plus
`gold_dev.dim`'s `dim_part`, `dim_warehouse`, `dim_supplier`, `dim_plant`, `dim_request_status`,
`dim_vehicle_model`. Business keys (`PART_ID`) ↔ surrogate keys (`PART_KEY`); dimension joins need
`IS_CURRENT = true`.

**DE's, but we write to it** — `fact_restock_request`: one row per part-line, appended by `persist_quote`
and updated by `apply_decision` / the app's `/complete` endpoint. Its `NOTE` column was added
out-of-band by `scripts/add_restock_note_column.py`, since DE owns the DDL and it is not tracked here.
`DECISION_REASON` and `EXPOSURE_AT_DECISION` were added for the redesign's suppression/re-surfacing
logic (see `selection.py` above) — see `docs/schema_changes_gold_dev_analytics.md` for the exact set of
columns this project added on top of DE's schema.

**Ours** —
- `quote_metadata` — per-quote Teams/Review-App fields (`summary_report`, `teams_message_id`, `databricks_preview_url`, `decision_comments`) that have no home in a per-part-line table. Created by `schema_bootstrap`.
- `part_position`, `parent_cascade`, `supplier_performance` — the corrected-measures layer, rebuilt wholesale by `refresh_positions`; the detectors' read surface.
- `scan_run_log` — one row per scan run, created on demand by `run_log.py`'s `CREATE TABLE IF NOT EXISTS`.
- `dim_bom`, `dim_model_bom`, `dim_supplier_contract` — named in `config.py`, read by `positions.py`. `dim_model_bom` (model → top-level assembly bridge) was added for the redesign's cascade rollup — without it a plan in vehicle models can't become a part requirement.
- `fact_plant_capacity` — named in `config.py` but **not actually read by anything** (confirmed by grep — its `PLANT_ID` values never even matched `dim_plant`'s real IDs). Left empty.
- `sim_events`, `sim_pair_scenarios` — existed to give an old randomized data generator backtest attribution. Retired along with that generator and left empty.

**`generate_sim_data`** (an even older job/notebook, and its dependency `src/agentic_restock/simulation.py`) has been retired and deleted — the module went missing from the repo before this redesign, and `fact_inventory_snapshot`/`fact_inventory_transaction`/`fact_procurement` on `gold_dev` are populated instead by `scripts/seed_demo_scenarios.py --rebuild-facts` (a hand-curated dataset, kept for `gold_dev` demo maintenance — see Commands above). The redesign's own dataset, on `gold_dev_analytics`, is generated separately by `scripts/generate_analytics_dataset.py` + `src/agentic_restock/generation/` — a fully deterministic generator (policy inputs decided in `generation/policy.py`) with its own assertion gate, documented in `docs/dataset_generator_spec.md`.

## DAB conventions

- `bundle: engine: direct` is set, inherited from when this bundle also managed `genie_spaces` resources (both retired now — see Retirement above); not re-verified whether jobs + apps alone would still need it.
- Do not hand-prefix resource `name`s. Each target declares `presets.name_prefix` (`"[dev] "` / `"[prod] "`) and the CLI prepends it.
- The `dev` target deliberately omits `mode: development`: dev mode forces `[dev <username>]` into every resource name and tag and rejects any `name_prefix` without that username. Its useful behaviors are declared explicitly instead (`pause_status: PAUSED` on the Lakeflow schedule).
- Notebook paths in a resource YAML resolve relative to **that YAML file**, not the bundle root — hence `../../notebooks/...`.
- `resources/*.yml` and `resources/**/*.yml` are globbed; a new file is picked up without editing `databricks.yml`.
- Never hardcode `supervisor_endpoint_name` in a job YAML. Endpoint names change on agent re-creation; `scripts/ensure_supervisor_agent.py` rewrites that default in place across every file in its `JOB_YAMLS` list (currently just `intelligence_job.yml` — `restock_decision_job.yml` never calls the Supervisor, and `lakeflow_trigger_job.yml` no longer exists) and `deploy_all.sh` redeploys afterwards. Add new jobs that call the Supervisor to that list.
- `presets.name_prefix` is **not** applied to `apps` resources — Databricks Apps names must be lowercase kebab-case, so the CLI skips the `[dev] `/`[prod] ` prefix there. Both targets point at the same workspace host, so a `prod` deploy would collide with `dev` on the app names `restock-review` and `mcp-inventory-actions`.
- `mcp-inventory-actions` must keep its `mcp-` prefix — the Supervisor's `app` tool type only accepts `mcp-`- or `agent-`-prefixed apps. `bundle deploy` deploys it like any other app; no separate connection-creation step is needed (unlike the old UC HTTP Connection approach). After first deploy, grant its service principal Unity Catalog access (see above) — `deploy_all.sh` prints a reminder with the resolved service principal id but does not apply the grant itself.
- `scripts/create_supervisor_agent.py` is the "as code" record of the agent's description/instructions/tool. `ensure_supervisor_agent.py` imports those constants and is the idempotent reconciler — run *that* one in automation. The two are now in sync (both describe a one-tool agent); the drift that used to exist between them (different tool ids for the same Genie Space) went away when the Genie Space did.

## Known drift in the working tree

Worth confirming before assuming a doc is current:

- **Several docs under `docs/` predate the redesign and describe removed architecture as current**: `docs/agent_bricks_mapping.md`, `docs/market_evidence_phase1.md`, `docs/uc_functions_reference.md`. They're useful for historical "why", not for current tool wiring or job structure — `docs/redesign_tracker.md`, `docs/end_to_end_walkthrough.md` and this file are current. None of the three above have been rewritten; that's a real follow-up, not an oversight to assume away.
- `src/agentic_restock/quote_persistence.py` and `integrations/teams_webhook.py` duplicate logic that now lives in `mcp-inventory-actions/server/tools.py` (quote writing, Adaptive Card building, `build_review_app_url`) — predates even the phase-1 redesign. Only `scripts/test_teams_card.py` still imports them, for local card rendering, and it renders a card shape Teams no longer receives (the simplified `Found N action items` card lives in the MCP server only). Use `tests/test_teams_card_builder.py` to check the real card. The MCP server is authoritative; don't add a caller to the local copies.
- `mcp-inventory-actions/README.md` + `Claude.md` and `restock-review/README.md` + `CLAUDE.md` each carry a real, current top section (fixed for the redesign) followed by unmodified "MCP Server - Hello World" / AppKit template boilerplate below a clearly marked divider. Read the top section and the code, not the template boilerplate below it.
- `docs/prd.md` + `docs/architecture.md` describe the original single-layer design and are kept as historical record. `prd_v2.md` is the 4-tier requirements doc behind the 16 deep-analysis functions. `docs/agentic_coarse_check_design.md` is superseded outright.
