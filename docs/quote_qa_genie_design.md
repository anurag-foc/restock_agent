# PM follow-up Q&A via Genie — feature design

**Status:** proposed, not built. **Owner:** TBD. **Depends on:** the current detection/quote
pipeline (`docs/intelligence_layer_design.md`, `docs/redesign_tracker.md`) — read those first if
the invariant this doc leans on isn't already familiar.

## Problem

A PM reading a quote in the Review App sees `RECOMMENDATION`, `OPTIONS CONSIDERED`, `EVIDENCE`,
`IF APPROVED AND WRONG` and `DECISION VALUE` — but only as static prose in
`quote_metadata.summary_report`. If they want to ask something the report didn't anticipate
("why this warehouse and not the other one with the same part?", "how does this supplier's
reject rate compare to the incumbent's?"), today there's nowhere to ask it — they either infer it
from the arithmetic already on screen or go find someone who can query the tables.

## What this is

A **Genie Agent** scoped to the tables that already hold a quote's evidence, queried directly
from the Review App when a PM asks a follow-up question about the quote or line they're
currently viewing. It answers from data that already exists — nothing it returns is computed by
an LLM; every number comes from a real `SELECT` against real tables.

## What this explicitly is not

- **Not attached to the intelligence Supervisor.** The Supervisor's tool set stays exactly the
  one `app` tool it has today (`inventory_intelligence_actions` → `persist_quote` /
  `send_human_review`), reconciled by `scripts/ensure_supervisor_agent.py`, which deletes
  anything else it finds. This feature adds a **second, unrelated** agent surface, not a tool on
  the first one.
- **Not a write path.** `restock-review/CLAUDE.md`'s house rule already says the app "does not
  make restock decisions, call the Supervisor Agent, or write quote data" — this feature doesn't
  touch that boundary. Genie here is read-only SQL (`SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN`
  only, per the Genie Agent creation rules), same as the app's existing analytics queries.
  Nothing about approving, rejecting, or persisting a line changes.
- **Not a general "ask anything" chatbot.** It is scoped to a handful of tables (below), sized
  well under Genie's practical/hard limits, and built for the questions PMs actually ask about a
  quote they're looking at — not open-ended exploration of the whole warehouse.

## Why this doesn't reopen the fabrication risk the redesign fixed

`docs/redesign_tracker.md` records two live incidents where an LLM was given room to compute or
choose a number and did so wrong. The structural fix was: **detectors compute every figure before
any model sees it; the model only writes prose around numbers it didn't produce.** That invariant
is about the *write* path (the brief → Supervisor → `persist_quote`) and stays untouched here.

Genie is a different shape of risk and a smaller one:

- It can only ever answer from **columns that already hold detector-computed values**
  (`EXPOSURE_AT_DECISION`, `ACTION_TYPE`, `SOURCE_WAREHOUSE_KEY`, `summary_report`, …). There is
  no slot for it to write a new number into anything a PM acts on.
- The realistic failure mode is a **wrong SQL join or filter** producing a misleading read of
  real data — a data-quality risk, not a fabrication risk. It's the same risk category as any
  analytics dashboard, and it's why the Genie Agent build process (see below) requires reviewing
  every attached table's grain and joins before shipping, not just pointing Genie at tables and
  hoping.
- It sits **downstream of, and outside, the automated pipeline** — nothing it says is persisted,
  scored, or fed back into `selection.py`'s suppression or the next scan run. A wrong answer is
  a PM being misinformed in a conversation, not a bad decision silently entering the ledger.

## Architecture

```
restock-review app (existing)
  QuoteDetailPage — PM viewing a specific quote/line
    "Ask about this quote" box (new)
      → POST /api/quotes/:quoteId/ask  { question }         (new endpoint, read-only)
          → Genie Conversation API: start-conversation / create-message
            against a NEW Genie Agent, "quote-followup-qa"
          → returns Genie's answer + the SQL it ran (surfaced for transparency)

quote-followup-qa (new Genie Agent — separate from everything below)
  tables:  quote_metadata, fact_restock_request  (join key: QUOTE_ID)
  reads:   Unity Catalog, via its own SQL warehouse
  writes:  nothing — read-only by construction (Genie Agent rule)

Supervisor Agent (unchanged) — exactly ONE tool, still
  inventory_intelligence_actions → persist_quote, send_human_review
```

The new Genie Agent does not appear anywhere in the intelligence job, the Supervisor, or
`apply_decision`. It is wired to exactly one caller: the Review App's new `/ask` endpoint.

## Data source design (the Genie Agent's context)

Per `databricks-genie-agents`' creation workflow, a Genie Agent's context is its **attached
tables + curated metadata**, not a document corpus — this is the thing to get right, not an
afterthought:

| table | why it's attached | key columns Genie needs described |
|---|---|---|
| `quote_metadata` | holds the full write-up per quote — `summary_report` is the `OPTIONS CONSIDERED` / `IF APPROVED AND WRONG` / evidence prose already written for this exact quote | `QUOTE_ID`, `summary_report`, `decision_comments` |
| `fact_restock_request` | the structured per-line facts behind the prose | `QUOTE_ID` (join key), `RESTOCK_REQUEST_ID`, `ACTION_TYPE`, `FINDING_TYPE`, `SUBJECT_KEY`, `PART_KEY`/`WAREHOUSE_KEY`, `SOURCE_WAREHOUSE_KEY` (donor, transfers only), `RECOMMENDED_SUPPLIER_KEY`, `EXPOSURE_AT_DECISION`, `DECISION_REASON`, `NOTE` |

Two tables, both already read by the app today — this is a **2-table Agent**, far under Genie's
30-object practical ceiling, which is the point: keep it tight rather than also attaching
`part_position` / `parent_cascade` / `supplier_performance` "in case a PM wants to go deeper."
That's a deliberate v1 cut — see Open questions.

Required column-level work before shipping (per the creation workflow's Design Priorities,
structured context before free text):
- `ACTION_TYPE` needs a description flagging it as **load-bearing**: `fact_restock_request` mixes
  purchases, transfers, and supplier/renegotiation-only rows in one table (see
  `docs/schema_changes_gold_dev_analytics.md` §1.4) — Genie must never sum `REQUESTED_QTY` across
  action types without filtering on it, the same rule that governs every other consumer of this
  table.
- `SOURCE_WAREHOUSE_KEY` / `RECOMMENDED_SUPPLIER_KEY` need descriptions explaining they're
  **nullable and action-type-specific** (a transfer has a donor and no supplier; most rows have
  neither).
- `EXPOSURE_AT_DECISION` needs a one-line description of what it is (`P(stockout) × consequence`,
  in rupees, frozen at decision time) so Genie doesn't confuse it with a live/current exposure.
- A handful of example questions and one or two example SQL pairs, scoped to "given a `QUOTE_ID`,
  explain/compare its lines" — not generic warehouse-wide examples.

## Review App integration

- New endpoint, read-only: `POST /api/quotes/:quoteId/ask { question: string }`. Scopes every
  Genie call to the quote on screen (pass `QUOTE_ID` as context/constraint, not as a free-text
  hint the model might drop).
- Auth: the server already resolves an authenticated `WorkspaceClient` via
  `getWorkspaceClient({})` from `@databricks/appkit` (used today for the SQL-warehouse statement
  execution in `server.ts`) — the same client can call the Genie Conversation API
  (`start-conversation` / `get-message`); no new credential plumbing needed if that client's
  scope already covers Genie. **Needs to be verified against `@databricks/appkit`'s actual
  Genie support before implementation** (see Open questions).
- UI: a small "Ask about this quote" box on `QuoteDetailPage`, answer rendered below it along
  with the SQL Genie ran — showing the query is what keeps this feature honest about being a
  lookup, not a second opinion.
- No caching (matches the existing `cache: { enabled: false }` policy in `server/server.ts` —
  same staleness argument applies: a PM asking about a decision they just made shouldn't get a
  stale answer).

## Deployment

Unlike the Supervisor Agent (SDK-only, reconciled by `scripts/ensure_supervisor_agent.py`
because it has no DAB resource type), **this bundle has deployed a `genie_spaces` DAB resource
before** — `bundle: engine: direct` in `databricks.yml` is inherited specifically from that era
(see CLAUDE.md's DAB conventions and Retirement section). So the new Genie Agent should be
deployable the normal way, as a `resources/genie_spaces/*.yml` (or current equivalent) file
alongside everything else — no new SDK reconciler script needed. Confirm the exact current DAB
resource type/schema for Genie Agents before writing the YAML; the two retired Genie Spaces
predate this doc and their resource definitions were removed with them.

## Non-goals (v1)

- No write capability of any kind.
- No attachment to the intelligence Supervisor or any Supervisor at all.
- No coverage of `part_position` / `parent_cascade` / `supplier_performance` — a PM asking "why"
  about a specific quote doesn't need the measurement layer, only the finding and its write-up.
- No cross-quote analytics ("how many transfers did we approve this month") — that's a
  dashboard/Genie-One question over `fact_restock_request` broadly, not this quote-scoped Agent.
- No changes to `apply_decision`, `selection.py`, or anything in the automated pipeline.

## Open questions

1. **Does `@databricks/appkit`'s `getWorkspaceClient` support the Genie Conversation API today?**
   Confirm before scoping implementation — if not, the endpoint needs its own auth path.
2. **Auth model for the Genie call**: on-behalf-of-user (the PM's own identity) vs. the app's
   service principal (like `mcp-inventory-actions` uses for its tool calls). OBO is the more
   natural fit for a human asking a question, but needs the PM's UC grants on `quote_metadata` /
   `fact_restock_request` to already be correct.
3. **Exact current DAB resource shape for a Genie Agent** — verify against the CLI/DABs docs
   rather than assuming the retired resource's shape still applies.
4. Should the answer surface a confidence/caveat when Genie's generated SQL returns zero rows
   (e.g., a malformed `QUOTE_ID`) rather than a plausible-looking empty answer?

## Rollout

1. Build and gate the Genie Agent standalone (CLI `create-space`, sample questions, a handful of
   real quotes as manual test cases) before touching the app.
2. Add the read-only `/api/quotes/:quoteId/ask` endpoint and UI box.
3. Dogfood on `gold_dev_analytics` quotes already in the approval queue.
4. No production cutover implications — this feature doesn't touch `gold_dev` vs
   `gold_dev_analytics` catalog routing at all; it reads whatever catalog `config.py` already
   resolves.
