import { createApp, analytics, server, jobs, getWorkspaceClient } from '@databricks/appkit';
import { z } from 'zod';

const CATALOG = process.env.GOLD_CATALOG || 'gold_dev';
const DIM_SCHEMA = process.env.GOLD_DIM_SCHEMA || 'dim';
const FACTS_SCHEMA = process.env.GOLD_FACTS_SCHEMA || 'supply_chain_analytics';

const FACT_RESTOCK_REQUEST = `${CATALOG}.${FACTS_SCHEMA}.fact_restock_request`;
const DIM_REQUEST_STATUS = `${CATALOG}.${DIM_SCHEMA}.dim_request_status`;

type DecisionBody = { decision: 'APPROVED' | 'REJECTED' };

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
            restock_request_key: z.string(),
            decision: z.enum(['APPROVED', 'REJECTED']),
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

      // Per-line-item Approve/Reject write-back. fact_restock_request's grain
      // is one row per part-line per quote (RESTOCK_REQUEST_KEY), so the
      // decision is scoped to a single line, not the whole quote — see
      // docs/architecture.md §6.1/§6.2.
      app.post('/api/quotes/:quoteId/lines/:lineKey/decision', async (req, res) => {
        const { quoteId, lineKey } = req.params;
        const { decision } = req.body as DecisionBody;

        if (decision !== 'APPROVED' && decision !== 'REJECTED') {
          res.status(400).json({ error: 'decision must be APPROVED or REJECTED' });
          return;
        }
        const lineKeyNum = Number(lineKey);
        if (!Number.isInteger(lineKeyNum)) {
          res.status(400).json({ error: 'lineKey must be an integer RESTOCK_REQUEST_KEY' });
          return;
        }

        const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
        if (!warehouseId) {
          res.status(500).json({ error: 'DATABRICKS_WAREHOUSE_ID is not configured' });
          return;
        }
        const client = getWorkspaceClient({});

        try {
          // Pre-flight only — this endpoint no longer writes. It validates the
          // line is real, belongs to the quote in the URL, and is still
          // undecided, so an obvious mistake gets a fast 404/409 instead of a
          // job run that fails a minute later. The authoritative status write
          // (and, for APPROVED, the Supervisor fulfillment turn) happens in
          // the restock_decision job, which is not behind the Apps proxy's
          // 120s request cap.
          const currentResult = await client.statementExecution.executeStatement({
            warehouse_id: warehouseId,
            wait_timeout: '30s',
            statement: `
              SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL
              FROM ${FACT_RESTOCK_REQUEST} frr
              JOIN ${DIM_REQUEST_STATUS} drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
              WHERE frr.RESTOCK_REQUEST_KEY = :lineKey AND frr.QUOTE_ID = :quoteId
            `,
            parameters: [
              { name: 'lineKey', type: 'BIGINT', value: String(lineKeyNum) },
              { name: 'quoteId', type: 'STRING', value: quoteId },
            ],
          });
          const currentRow = currentResult.result?.data_array?.[0];
          if (!currentRow) {
            res.status(404).json({ error: `Line ${lineKey} not found on quote ${quoteId}` });
            return;
          }
          const [currentStatus, urgency] = currentRow;
          if (currentStatus !== 'PENDING_APPROVAL') {
            res.status(409).json({ error: `Line already decided (status: ${currentStatus})` });
            return;
          }

          const result = await appkit.jobs('restock_decision').runNow({
            restock_request_key: String(lineKeyNum),
            decision,
          });
          if (!result.ok) {
            console.error('Failed to trigger restock_decision job', result);
            res.status(502).json({ error: `Could not start the decision job: ${result.message}` });
            return;
          }

          res.json({
            ok: true,
            quoteId,
            lineKey: lineKeyNum,
            decision,
            urgency,
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
