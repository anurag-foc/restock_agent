import { createApp, analytics, server, jobs, getWorkspaceClient } from '@databricks/appkit';
import { z } from 'zod';
import { coerceSetting, SETTINGS_BY_KEY } from '../shared/settingsSpec';

const CATALOG = process.env.GOLD_CATALOG || 'gold_dev';
const DIM_SCHEMA = process.env.GOLD_DIM_SCHEMA || 'dim';
const FACTS_SCHEMA = process.env.GOLD_FACTS_SCHEMA || 'supply_chain_analytics';

const FACT_RESTOCK_REQUEST = `${CATALOG}.${FACTS_SCHEMA}.fact_restock_request`;
const APP_SETTINGS = `${CATALOG}.${FACTS_SCHEMA}.app_settings`;
const DIM_REQUEST_STATUS = `${CATALOG}.${DIM_SCHEMA}.dim_request_status`;

type LineDecision = { lineKey: number; decision: 'APPROVED' | 'REJECTED'; note?: string; reason?: ReasonCode };
// The reason code is what changes the system's behaviour on the next run -- CANNOT_ACT_NOW is
// a snooze that returns in two weeks, NOT_A_PROBLEM closes the finding until its value grows.
// Free text cannot carry that: "no" and "not now" look identical and mean opposite things.
const REASON_CODES = ['NOT_A_PROBLEM', 'ALREADY_HANDLED', 'CANNOT_ACT_NOW', 'NUMBERS_WRONG', 'OTHER'] as const;
type ReasonCode = (typeof REASON_CODES)[number];
type DecisionsBody = {
  decisions: Array<{ lineKey: number | string; decision: string; note?: string; reason?: string }>;
};

createApp({
  plugins: [
    analytics(),
    server(),
    // Real-time re-validation after approval runs as a job, not inline in
    // this handler — the Apps reverse proxy hard-caps requests at 120s
    // (non-configurable) and a cold Supervisor+Genie round-trip has been
    // measured at ~110s. See notebooks/restock_agent/restock_agent.py.
    jobs({
      jobs: {
        restock_decision: {
          taskType: 'notebook',
          params: z.object({
            decisions_json: z.string(),
          }),
        },
        // A simulation run builds the generated world (~10s) and scores each
        // engine against it (~2s each), which is comfortably past what the
        // Apps proxy allows inline. Results land in sim_run / sim_run_pair and
        // the page reads them back through analytics like every other screen.
        simulation: {
          taskType: 'notebook',
          params: z.object({
            engines: z.string(),
            budget: z.string(),
            label: z.string(),
          }),
        },
      },
    }),
  ],
  // Cached analytics results would hide a decision a PM just made (approve/
  // reject writes go straight to fact_restock_request, bypassing any cache
  // entry for quote_lines/pending_quotes) — this is a low-traffic approval
  // screen, so correctness beats shaving a warehouse round-trip.
  cache: { enabled: false },
  async onPluginsReady(appkit) {
    appkit.server.extend((app) => {
      // Inventory Intelligence's action tools (persist_quote, send_human_review,
      // fulfill_restock_request) used to be served from this app at
      // POST /api/mcp, reached via a UC HTTP Connection. They now live in
      // the dedicated mcp-inventory-actions app, attached to the Supervisor
      // directly via the `app` tool type -- see
      // mcp-inventory-actions/server/tools.py and
      // docs/agent_bricks_mapping.md. This app is UI-only.

      // Batched Approve/Reject write-back for the whole quote. The PM stages
      // a decision (and optional note) per line in the UI, then Final Submit
      // sends every staged line here in one request, which becomes one
      // restock_decision job run covering the whole batch. fact_restock_request's
      // grain is still one row per part-line (RESTOCK_REQUEST_KEY) — see
      // docs/architecture.md §6.1/§6.2 — this endpoint just fans one job
      // trigger out over all of them instead of one job run per click.
      app.post('/api/quotes/:quoteId/decisions', async (req, res) => {
        const { quoteId } = req.params;
        const { decisions: rawDecisions } = req.body as DecisionsBody;

        if (!Array.isArray(rawDecisions) || rawDecisions.length === 0) {
          res.status(400).json({ error: 'decisions must be a non-empty array' });
          return;
        }

        const decisions: LineDecision[] = [];
        for (const d of rawDecisions) {
          const lineKeyNum = Number(d.lineKey);
          if (!Number.isInteger(lineKeyNum)) {
            res.status(400).json({ error: `lineKey must be an integer RESTOCK_REQUEST_KEY, got: ${d.lineKey}` });
            return;
          }
          if (d.decision !== 'APPROVED' && d.decision !== 'REJECTED') {
            res.status(400).json({ error: `decision must be APPROVED or REJECTED, got: ${d.decision}` });
            return;
          }
          const reason = d.reason?.trim().toUpperCase();
          // Required on a rejection, where it decides whether the finding ever comes back.
          // Optional on an approval, where there is nothing to suppress.
          if (d.decision === 'REJECTED' && !reason) {
            res.status(400).json({ error: `line ${lineKeyNum}: a rejection needs a reason` });
            return;
          }
          if (reason && !REASON_CODES.includes(reason as ReasonCode)) {
            res.status(400).json({ error: `reason must be one of ${REASON_CODES.join(', ')}, got: ${reason}` });
            return;
          }
          decisions.push({
            lineKey: lineKeyNum,
            decision: d.decision,
            note: d.note?.trim() || undefined,
            reason: (reason as ReasonCode) || undefined,
          });
        }

        const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
        if (!warehouseId) {
          res.status(500).json({ error: 'DATABRICKS_WAREHOUSE_ID is not configured' });
          return;
        }
        const client = getWorkspaceClient({});

        try {
          // Pre-flight only — this endpoint no longer writes. It validates every
          // staged line is real, belongs to the quote in the URL, and is still
          // undecided, so an obvious mistake gets a fast 404/409 for the whole
          // batch instead of a job run that fails a minute later. The
          // authoritative status write (and, for APPROVED lines, the Supervisor
          // fulfillment turn) happens in the restock_decision job, which is not
          // behind the Apps proxy's 120s request cap.
          const lineKeys = decisions.map((d) => d.lineKey);
          const currentResult = await client.statementExecution.executeStatement({
            warehouse_id: warehouseId,
            wait_timeout: '30s',
            statement: `
              SELECT frr.RESTOCK_REQUEST_KEY, drs.REQUEST_STATUS, drs.URGENCY_LEVEL
              FROM ${FACT_RESTOCK_REQUEST} frr
              JOIN ${DIM_REQUEST_STATUS} drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
              WHERE frr.QUOTE_ID = :quoteId AND frr.RESTOCK_REQUEST_KEY IN (${lineKeys.map((_, i) => `:lineKey${i}`).join(', ')})
            `,
            parameters: [
              { name: 'quoteId', type: 'STRING', value: quoteId },
              ...lineKeys.map((k, i) => ({ name: `lineKey${i}`, type: 'BIGINT', value: String(k) })),
            ],
          });
          const rowsByKey = new Map((currentResult.result?.data_array ?? []).map((row) => [Number(row[0]), row]));

          const notFound = lineKeys.filter((k) => !rowsByKey.has(k));
          if (notFound.length > 0) {
            res.status(404).json({ error: `Line(s) not found on quote ${quoteId}: ${notFound.join(', ')}` });
            return;
          }
          // A line is re-decidable from PENDING_APPROVAL (first decision) or
          // NEEDS_REVIEW (the fulfillment guardrail flagged it post-approval;
          // the PM is deciding whether to retry or cancel it) -- see
          // apply_decision.py. Anything else is a stale/duplicate submit.
          const notDecidable = lineKeys
            .map((k) => ({ k, status: rowsByKey.get(k)![1] }))
            .filter((r) => r.status !== 'PENDING_APPROVAL' && r.status !== 'NEEDS_REVIEW');
          if (notDecidable.length > 0) {
            res.status(409).json({
              error: `Line(s) already decided: ${notDecidable.map((r) => `${r.k} (${r.status})`).join(', ')}`,
            });
            return;
          }

          const result = await appkit.jobs('restock_decision').runNow({
            decisions_json: JSON.stringify(
              decisions.map((d) => ({
                restock_request_key: d.lineKey,
                decision: d.decision,
                note: d.note ?? '',
                reason: d.reason ?? '',
              }))
            ),
          });
          if (!result.ok) {
            console.error('Failed to trigger restock_decision job', result);
            res.status(502).json({ error: `Could not start the decision job: ${result.message}` });
            return;
          }

          res.json({
            ok: true,
            quoteId,
            lines: decisions.map((d) => ({ lineKey: d.lineKey, decision: d.decision })),
            decisionRunId: result.data.run_id,
          });
        } catch (err) {
          console.error('Decision trigger failed', err);
          res.status(502).json({ error: 'Failed to start the restock decision job' });
        }
      });

      // Mark a single FULFILLING line COMPLETED once the PM confirms physical
      // receipt. Unlike the approve/reject decision above, this carries no
      // judgment call for the Supervisor to re-check -- it's a deterministic
      // status flip, so it writes directly here instead of going through a
      // job run. Idempotent: only acts on a line currently FULFILLING, and
      // appends rather than overwrites NOTE so the approval-stage note isn't
      // lost.
      app.post('/api/lines/:lineKey/complete', async (req, res) => {
        const lineKeyNum = Number(req.params.lineKey);
        if (!Number.isInteger(lineKeyNum)) {
          res.status(400).json({ error: 'lineKey must be an integer RESTOCK_REQUEST_KEY' });
          return;
        }
        const note = typeof req.body?.note === 'string' ? req.body.note.trim() : '';

        const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
        if (!warehouseId) {
          res.status(500).json({ error: 'DATABRICKS_WAREHOUSE_ID is not configured' });
          return;
        }
        const client = getWorkspaceClient({});

        try {
          const currentResult = await client.statementExecution.executeStatement({
            warehouse_id: warehouseId,
            wait_timeout: '30s',
            statement: `
              SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL
              FROM ${FACT_RESTOCK_REQUEST} frr
              JOIN ${DIM_REQUEST_STATUS} drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
              WHERE frr.RESTOCK_REQUEST_KEY = :lineKey
            `,
            parameters: [{ name: 'lineKey', type: 'BIGINT', value: String(lineKeyNum) }],
          });
          const currentRow = currentResult.result?.data_array?.[0];
          if (!currentRow) {
            res.status(404).json({ error: `Line ${lineKeyNum} not found` });
            return;
          }
          const [currentStatus, urgency] = currentRow;
          if (currentStatus !== 'FULFILLING') {
            res.status(409).json({ error: `Line is ${currentStatus}, not FULFILLING -- cannot complete` });
            return;
          }

          await client.statementExecution.executeStatement({
            warehouse_id: warehouseId,
            wait_timeout: '30s',
            statement: `
              UPDATE ${FACT_RESTOCK_REQUEST}
              SET
                REQUEST_STATUS_KEY = (
                  SELECT MIN(REQUEST_STATUS_KEY) FROM ${DIM_REQUEST_STATUS}
                  WHERE REQUEST_STATUS = 'COMPLETED' AND URGENCY_LEVEL = :urgency
                ),
                FULFILLED_DATE_KEY = CAST(date_format(current_date(), 'yyyyMMdd') AS INT)
                ${note ? ", NOTE = CONCAT(COALESCE(NOTE, ''), CASE WHEN NOTE IS NULL OR NOTE = '' THEN '' ELSE '\n\n' END, :note)" : ''}
              WHERE RESTOCK_REQUEST_KEY = :lineKey
            `,
            parameters: [
              { name: 'urgency', type: 'STRING', value: urgency },
              ...(note ? [{ name: 'note', type: 'STRING', value: note }] : []),
              { name: 'lineKey', type: 'BIGINT', value: String(lineKeyNum) },
            ],
          });

          res.json({ ok: true, lineKey: lineKeyNum, status: 'COMPLETED' });
        } catch (err) {
          console.error('Complete line failed', err);
          res.status(502).json({ error: 'Failed to mark the line completed' });
        }
      });

      // Start a simulation. Triggers only -- like the decision endpoint, it does not compute
      // and does not write. The results land in sim_run / sim_run_pair and the page reads
      // them back through analytics.
      //
      // The engine list is validated against the settings registry rather than a local
      // constant, so an engine the pipeline cannot dispatch can never be queued: the job
      // would otherwise fall back to `automatic` and silently report a run under the wrong
      // label, which is worse than refusing.
      app.post('/api/simulations', async (req, res) => {
        const body = req.body as { engines?: unknown; budget?: unknown; label?: unknown };
        const allowed = SETTINGS_BY_KEY['consumption_model'].choices?.map((c) => c.value) ?? [];

        const engines = Array.isArray(body?.engines) ? body.engines.map(String) : [];
        if (engines.length === 0) {
          res.status(400).json({ error: 'Pick at least one engine to run' });
          return;
        }
        const unknown = engines.filter((e) => !allowed.includes(e));
        if (unknown.length > 0) {
          res.status(400).json({ error: `Not a known engine: ${unknown.join(', ')}` });
          return;
        }

        let budget = '';
        if (body?.budget !== undefined && body.budget !== null && body.budget !== '') {
          const parsed = Number(body.budget);
          const spec = SETTINGS_BY_KEY['items_per_notification'];
          if (!Number.isInteger(parsed) || parsed < (spec.min ?? 1) || parsed > (spec.max ?? 10)) {
            res.status(400).json({ error: `Budget must be a whole number between ${spec.min} and ${spec.max}` });
            return;
          }
          budget = String(parsed);
        }

        try {
          const result = await appkit.jobs('simulation').runNow({
            engines: engines.join(','),
            budget,
            label: typeof body?.label === 'string' && body.label.trim() ? body.label.trim() : 'Engine comparison',
          });
          if (!result.ok) {
            console.error('Failed to trigger simulation job', result);
            res.status(502).json({ error: `Could not start the simulation: ${result.message}` });
            return;
          }
          res.json({ ok: true, runId: result.data.run_id, engines });
        } catch (err) {
          console.error('Simulation trigger failed', err);
          res.status(502).json({ error: 'Failed to start the simulation job' });
        }
      });

      // The admin panel's only write. Append-only: a change is a new row, so the table is
      // its own change history and a revert is a write rather than a delete.
      //
      // Validated here as well as in the pipeline, against the registry mirrored from
      // src/agentic_restock/settings.py. Letting an out-of-range value land would not break
      // anything loudly -- the pipeline falls back to the default on read -- it would just
      // mean a client changed a setting and nothing happened, which is the hardest kind of
      // bug to be told about.
      app.post('/api/settings', async (req, res) => {
        const body = req.body as { settings?: unknown } | undefined;
        const updates = body?.settings;
        if (!updates || typeof updates !== 'object' || Array.isArray(updates)) {
          res.status(400).json({ error: 'Body must be { settings: { key: value } }' });
          return;
        }

        const entries = Object.entries(updates as Record<string, unknown>);
        if (entries.length === 0) {
          res.status(400).json({ error: 'No settings supplied' });
          return;
        }

        const checked: Array<{ key: string; value: string }> = [];
        for (const [key, value] of entries) {
          if (!SETTINGS_BY_KEY[key]) {
            res.status(400).json({ error: `Unknown setting: ${key}` });
            return;
          }
          try {
            checked.push({ key, value: JSON.stringify(coerceSetting(key, value)) });
          } catch (err) {
            res.status(400).json({ error: err instanceof Error ? err.message : `Invalid value for ${key}` });
            return;
          }
        }

        const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
        if (!warehouseId) {
          res.status(500).json({ error: 'DATABRICKS_WAREHOUSE_ID is not configured' });
          return;
        }
        // The signed-in user, so the panel's "last changed by" line names a person. Falls
        // back rather than failing: an unattributed change beats a refused one.
        const updatedBy =
          (req.headers['x-forwarded-email'] as string) ||
          (req.headers['x-forwarded-preferred-username'] as string) ||
          'unknown';

        const client = getWorkspaceClient({});
        try {
          // One INSERT for the batch. Settings are read as a set at job start, so a partial
          // write would leave the pipeline running a policy nobody chose.
          const values = checked
            .map((_, i) => `(:key${i}, 'GLOBAL', NULL, :value${i}, current_timestamp(), :updatedBy)`)
            .join(', ');
          await client.statementExecution.executeStatement({
            warehouse_id: warehouseId,
            wait_timeout: '30s',
            statement: `
              INSERT INTO ${APP_SETTINGS}
                (setting_key, scope_type, scope_id, setting_value, updated_at, updated_by)
              VALUES ${values}
            `,
            parameters: [
              ...checked.flatMap((c, i) => [
                { name: `key${i}`, type: 'STRING', value: c.key },
                { name: `value${i}`, type: 'STRING', value: c.value },
              ]),
              { name: 'updatedBy', type: 'STRING', value: updatedBy },
            ],
          });
          res.json({ ok: true, updated: checked.map((c) => c.key) });
        } catch (err) {
          console.error('Settings write failed', err);
          res.status(502).json({ error: 'Failed to save settings' });
        }
      });
    });
  },
}).catch(console.error);
