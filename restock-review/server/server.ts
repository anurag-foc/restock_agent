import { createApp, analytics, server, jobs, getWorkspaceClient } from '@databricks/appkit';
import { z } from 'zod';

const CATALOG = process.env.GOLD_CATALOG || 'gold_dev';
const DIM_SCHEMA = process.env.GOLD_DIM_SCHEMA || 'dim';
const FACTS_SCHEMA = process.env.GOLD_FACTS_SCHEMA || 'supply_chain_analytics';

const FACT_RESTOCK_REQUEST = `${CATALOG}.${FACTS_SCHEMA}.fact_restock_request`;
const DIM_REQUEST_STATUS = `${CATALOG}.${DIM_SCHEMA}.dim_request_status`;

type LineDecision = { lineKey: number; decision: 'APPROVED' | 'REJECTED'; note?: string };
type DecisionsBody = { decisions: Array<{ lineKey: number | string; decision: string; note?: string }> };

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
          decisions.push({ lineKey: lineKeyNum, decision: d.decision, note: d.note?.trim() || undefined });
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
          const alreadyDecided = lineKeys
            .map((k) => ({ k, status: rowsByKey.get(k)![1] }))
            .filter((r) => r.status !== 'PENDING_APPROVAL');
          if (alreadyDecided.length > 0) {
            res.status(409).json({
              error: `Line(s) already decided: ${alreadyDecided.map((r) => `${r.k} (${r.status})`).join(', ')}`,
            });
            return;
          }

          const result = await appkit.jobs('restock_decision').runNow({
            decisions_json: JSON.stringify(
              decisions.map((d) => ({ restock_request_key: d.lineKey, decision: d.decision, note: d.note ?? '' }))
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
    });
  },
}).catch(console.error);
