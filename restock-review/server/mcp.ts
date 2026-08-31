/**
 * MCP server exposing Restockify's *action* tools to the Supervisor Agent.
 *
 * Why this exists: Unity Catalog functions cannot perform DML (a SQL function
 * body with INSERT fails to parse) and UC Python UDFs have no network egress
 * (verified: urllib returns URLError). So the write/notify tools the Supervisor
 * needs cannot be `uc_function` tools. Databricks' supported path for
 * action tools is an MCP server behind a UC HTTP Connection
 * (`is_mcp_connection 'true'`), attached to the agent as a `uc_connection`
 * tool — which is what this module serves at POST /api/mcp.
 *
 * Every tool here is IDEMPOTENT by construction. The Supervisor is an LLM: it
 * may retry, re-run, or call a tool twice in an ambiguous turn. Duplicate rows
 * in fact_restock_request mean duplicate procurement, so idempotency is
 * enforced here in code rather than assumed of the model:
 *   - persist_quote           -> quote_id is a deterministic hash of the
 *                                candidate set + date; existing quote is
 *                                returned instead of re-inserted.
 *   - send_human_review       -> no-ops if quote_metadata.teams_message_id is
 *                                already set.
 *   - fulfill_restock_request -> only transitions a line that is currently
 *                                APPROVED; re-calls are reported, not re-applied.
 *
 * fulfill_restock_request also never trusts the model for arithmetic: it
 * reads live stock and computes CONFIRMED_QTY/VARIANCE_QTY itself. The
 * caller (Genie via the Supervisor) supplies exactly one thing — a
 * PROCEED/NEEDS_REVIEW judgment call for a request that may have sat
 * PENDING_APPROVAL a while before being approved, during which the stock
 * situation can have already changed (replenished, covered by a newer PO,
 * demand collapsed). PROCEED writes FULFILLING at the originally-approved
 * quantity; NEEDS_REVIEW flags it for a human instead of writing the
 * transition blindly.
 */

import { createHash } from 'node:crypto';
import type { Request, Response } from 'express';
import { getWorkspaceClient } from '@databricks/appkit';

const CATALOG = process.env.GOLD_CATALOG || 'gold_dev';
const DIM_SCHEMA = process.env.GOLD_DIM_SCHEMA || 'dim';
const FACTS_SCHEMA = process.env.GOLD_FACTS_SCHEMA || 'supply_chain_analytics';

const FACT_RESTOCK_REQUEST = `${CATALOG}.${FACTS_SCHEMA}.fact_restock_request`;
const QUOTE_METADATA = `${CATALOG}.${FACTS_SCHEMA}.quote_metadata`;
const DIM_REQUEST_STATUS = `${CATALOG}.${DIM_SCHEMA}.dim_request_status`;
const DIM_PART = `${CATALOG}.${DIM_SCHEMA}.dim_part`;
const DIM_WAREHOUSE = `${CATALOG}.${DIM_SCHEMA}.dim_warehouse`;

type SqlParam = { name: string; type?: string; value: string };

async function runSql(statement: string, parameters: SqlParam[] = []): Promise<string[][]> {
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID is not configured');
  const client = getWorkspaceClient({});
  const res = await client.statementExecution.executeStatement({
    warehouse_id: warehouseId,
    wait_timeout: '50s',
    statement,
    parameters,
  });
  if (res.status?.state !== 'SUCCEEDED') {
    throw new Error(`SQL ${res.status?.state}: ${res.status?.error?.message ?? 'unknown error'}`);
  }
  return res.result?.data_array ?? [];
}

function todayDateKey(): number {
  const now = new Date();
  return Number(
    `${now.getUTCFullYear()}${String(now.getUTCMonth() + 1).padStart(2, '0')}${String(now.getUTCDate()).padStart(2, '0')}`
  );
}

// ── Types ────────────────────────────────────────────────────────────────────

type Candidate = {
  item_id?: string;
  warehouse_id?: string;
  current_stock_qty?: number | string;
  reorder_point_qty?: number | string;
  suggested_reorder_qty?: number | string;
  initial_urgency?: string;
  signal_type?: string;
  item_name?: string;
  days_to_stockout?: number | string | null;
  threatened_assembly?: string | null;
};

function asInt(v: unknown, fallback = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

/**
 * Deterministic quote id: same candidate set on the same UTC day always
 * produces the same id, so a repeated tool call collides with the existing
 * row instead of creating a second quote.
 */
function deterministicQuoteId(candidates: Candidate[]): string {
  const fingerprint = candidates
    .map((c) => `${c.item_id ?? ''}@${c.warehouse_id ?? ''}:${asInt(c.suggested_reorder_qty)}`)
    .sort()
    .join('|');
  const digest = createHash('sha256').update(fingerprint).digest('hex').slice(0, 6).toUpperCase();
  return `QT-${todayDateKey()}-${digest}`;
}

// ── Tool: persist_quote ──────────────────────────────────────────────────────

async function persistQuote(args: { candidates_json: string; summary_report: string }) {
  let candidates: Candidate[];
  try {
    candidates = JSON.parse(args.candidates_json);
  } catch {
    throw new Error('candidates_json must be a JSON array of candidate objects');
  }
  if (!Array.isArray(candidates) || candidates.length === 0) {
    throw new Error('candidates_json must be a non-empty JSON array');
  }

  const quoteId = deterministicQuoteId(candidates);

  // Idempotency gate: this exact candidate set was already persisted today.
  const existing = await runSql(`SELECT quote_id FROM ${QUOTE_METADATA} WHERE quote_id = :quoteId`, [
    { name: 'quoteId', type: 'STRING', value: quoteId },
  ]);
  if (existing.length > 0) {
    return {
      quote_id: quoteId,
      created: false,
      note: 'Quote already exists for this candidate set today; no rows written.',
    };
  }

  await runSql(
    `INSERT INTO ${QUOTE_METADATA} (quote_id, summary_report, created_by, created_at, updated_at)
     VALUES (:quoteId, :summaryReport, 'supervisor_agent', current_timestamp(), current_timestamp())`,
    [
      { name: 'quoteId', type: 'STRING', value: quoteId },
      { name: 'summaryReport', type: 'STRING', value: args.summary_report ?? '' },
    ]
  );

  // One fact row per part-line. REQUEST_STATUS_KEY is resolved per line from
  // (PENDING_APPROVAL, that line's urgency) — the dim is keyed on
  // status x urgency x decision, so a single hardcoded key would mislabel
  // every non-CRITICAL line.
  const todayKey = todayDateKey();
  let inserted = 0;
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    const requestId = `REQ-${quoteId.replace('QT-', '')}-${i + 1}`;
    await runSql(
      `INSERT INTO ${FACT_RESTOCK_REQUEST} (
         RESTOCK_REQUEST_KEY, QUOTE_ID, RESTOCK_REQUEST_ID, REQUESTED_DATE_KEY,
         PART_KEY, WAREHOUSE_KEY, REQUEST_STATUS_KEY,
         CURRENT_STOCK_QTY, REORDER_POINT_QTY, REQUESTED_QTY, DW_LOADED_AT
       )
       SELECT
         (SELECT COALESCE(MAX(RESTOCK_REQUEST_KEY), 0) FROM ${FACT_RESTOCK_REQUEST}) + 1,
         :quoteId, :requestId, :todayKey,
         dp.PART_KEY, dw.WAREHOUSE_KEY,
         (SELECT MIN(REQUEST_STATUS_KEY) FROM ${DIM_REQUEST_STATUS}
           WHERE REQUEST_STATUS = 'PENDING_APPROVAL' AND URGENCY_LEVEL = :urgency),
         :currentStock, :reorderPoint, :requestedQty, current_timestamp()
       FROM ${DIM_PART} dp
       CROSS JOIN ${DIM_WAREHOUSE} dw
       WHERE dp.PART_ID = :partId AND dp.IS_CURRENT = true
         AND dw.WAREHOUSE_ID = :warehouseId`,
      [
        { name: 'quoteId', type: 'STRING', value: quoteId },
        { name: 'requestId', type: 'STRING', value: requestId },
        { name: 'todayKey', type: 'INT', value: String(todayKey) },
        { name: 'urgency', type: 'STRING', value: c.initial_urgency ?? 'CRITICAL' },
        { name: 'currentStock', type: 'INT', value: String(asInt(c.current_stock_qty)) },
        { name: 'reorderPoint', type: 'INT', value: String(asInt(c.reorder_point_qty)) },
        { name: 'requestedQty', type: 'INT', value: String(asInt(c.suggested_reorder_qty)) },
        { name: 'partId', type: 'STRING', value: c.item_id ?? '' },
        { name: 'warehouseId', type: 'STRING', value: c.warehouse_id ?? '' },
      ]
    );
    inserted++;
  }

  return { quote_id: quoteId, created: true, lines_written: inserted };
}

// ── Tool: send_human_review ──────────────────────────────────────────────────

function buildAdaptiveCard(quoteId: string, summary: string, reviewUrl: string) {
  const trimmed = summary.length > 1200 ? `${summary.slice(0, 1200)}\n\n_… (full report in Databricks)_` : summary;
  return {
    type: 'message',
    attachments: [
      {
        contentType: 'application/vnd.microsoft.card.adaptive',
        content: {
          type: 'AdaptiveCard',
          $schema: 'http://adaptivecards.io/schemas/adaptive-card.json',
          version: '1.4',
          body: [
            {
              type: 'TextBlock',
              text: '🏭 Restock Quote Awaiting Approval',
              weight: 'Bolder',
              size: 'Large',
              wrap: true,
            },
            { type: 'TextBlock', text: `Quote ${quoteId}`, weight: 'Bolder', size: 'Small', isSubtle: true },
            { type: 'TextBlock', text: trimmed, wrap: true, size: 'Small' },
            {
              type: 'TextBlock',
              text: '⚠️ Approve or reject each part-line in the Databricks Review App.',
              wrap: true,
              size: 'Small',
              color: 'Warning',
            },
          ],
          actions: [
            { type: 'Action.OpenUrl', title: '📋 Review in Databricks', url: reviewUrl, style: 'positive' },
          ],
        },
      },
    ],
  };
}

async function sendHumanReview(args: { quote_id: string; summary_report: string; review_url: string }) {
  const { quote_id: quoteId, summary_report: summary, review_url: reviewUrl } = args;

  // Idempotency gate: a card already went out for this quote.
  const existing = await runSql(
    `SELECT teams_message_id FROM ${QUOTE_METADATA} WHERE quote_id = :quoteId`,
    [{ name: 'quoteId', type: 'STRING', value: quoteId }]
  );
  if (existing.length === 0) {
    throw new Error(`No quote_metadata row for ${quoteId} — call persist_quote first.`);
  }
  if (existing[0][0]) {
    return { quote_id: quoteId, sent: false, teams_message_id: existing[0][0], note: 'Card already sent.' };
  }

  const webhookUrl = process.env.TEAMS_WEBHOOK_URL;
  const payload = buildAdaptiveCard(quoteId, summary, reviewUrl);
  let teamsMessageId: string;
  let dryRun = false;

  if (!webhookUrl) {
    // Same posture as the notebook path: never fail the workflow because a
    // webhook isn't configured — record a dry-run marker instead.
    dryRun = true;
    teamsMessageId = `dry-run-${Date.now().toString(36)}`;
  } else {
    const resp = await fetch(webhookUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      throw new Error(`Teams webhook returned HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
    }
    teamsMessageId = `teams-${quoteId}-${Date.now().toString(36)}`;
  }

  await runSql(
    `UPDATE ${QUOTE_METADATA}
     SET teams_message_id = :messageId, teams_sent_at = current_timestamp(),
         databricks_preview_url = :reviewUrl, updated_at = current_timestamp()
     WHERE quote_id = :quoteId`,
    [
      { name: 'messageId', type: 'STRING', value: teamsMessageId },
      { name: 'reviewUrl', type: 'STRING', value: reviewUrl },
      { name: 'quoteId', type: 'STRING', value: quoteId },
    ]
  );

  return { quote_id: quoteId, sent: true, dry_run: dryRun, teams_message_id: teamsMessageId };
}

// ── Tool: fulfill_restock_request ────────────────────────────────────────────
//
// The quantity math is deliberately NOT an argument the caller supplies —
// CONFIRMED_QTY/VARIANCE_QTY are read from a live fact_inventory_snapshot
// query inside this function, not parsed out of an LLM's prose. Genie/the
// Supervisor contribute exactly one thing here: the PROCEED vs NEEDS_REVIEW
// judgment call — has the situation drifted enough since approval (someone
// else already restocked it, an emergency PO already landed, demand
// collapsed) that a human should look again before this becomes a real
// order. That is a genuine judgment call; the arithmetic behind it is not.

async function fulfillRestockRequest(args: {
  restock_request_key: number | string;
  proceed: boolean;
  note?: string;
}) {
  const lineKey = asInt(args.restock_request_key, -1);
  if (lineKey < 0) throw new Error('restock_request_key must be an integer');
  if (typeof args.proceed !== 'boolean') throw new Error('proceed must be a boolean');

  const rows = await runSql(
    `SELECT drs.REQUEST_STATUS, drs.URGENCY_LEVEL, frr.QUOTE_ID, frr.PART_KEY, frr.WAREHOUSE_KEY,
            frr.REQUESTED_QTY, frr.CURRENT_STOCK_QTY
     FROM ${FACT_RESTOCK_REQUEST} frr
     JOIN ${DIM_REQUEST_STATUS} drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
     WHERE frr.RESTOCK_REQUEST_KEY = :lineKey`,
    [{ name: 'lineKey', type: 'BIGINT', value: String(lineKey) }]
  );
  if (rows.length === 0) throw new Error(`No fact_restock_request row with RESTOCK_REQUEST_KEY=${lineKey}`);

  const [currentStatus, urgency, quoteId, partKey, warehouseKey, requestedQty, quoteTimeStock] = rows[0];
  // Idempotency gate: only an APPROVED line may transition. A repeat call on
  // a line that already moved to FULFILLING or NEEDS_REVIEW reports, it does
  // not re-apply.
  if (currentStatus !== 'APPROVED') {
    return {
      restock_request_key: lineKey,
      transitioned: false,
      current_status: currentStatus,
      note: `Line is ${currentStatus}, not APPROVED — no transition applied.`,
    };
  }

  // Live stock, deterministically. Same latest-row-per-key dedup used
  // everywhere else in this repo against this daily fact.
  const snapshotRows = await runSql(
    `SELECT QUANTITY_ON_HAND
     FROM ${CATALOG}.${FACTS_SCHEMA}.fact_inventory_snapshot
     WHERE PART_KEY = :partKey AND WAREHOUSE_KEY = :warehouseKey
     QUALIFY ROW_NUMBER() OVER (PARTITION BY PART_KEY, WAREHOUSE_KEY ORDER BY SNAPSHOT_DATE_KEY DESC) = 1`,
    [
      { name: 'partKey', type: 'BIGINT', value: String(partKey) },
      { name: 'warehouseKey', type: 'BIGINT', value: String(warehouseKey) },
    ]
  );
  const currentStock = snapshotRows.length > 0 ? asInt(snapshotRows[0][0]) : null;
  const varianceQty = currentStock !== null ? currentStock - asInt(quoteTimeStock) : null;

  const newStatus = args.proceed ? 'FULFILLING' : 'NEEDS_REVIEW';
  const setClauses = [
    `REQUEST_STATUS_KEY = (SELECT MIN(REQUEST_STATUS_KEY) FROM ${DIM_REQUEST_STATUS} WHERE REQUEST_STATUS = :newStatus AND URGENCY_LEVEL = :urgency)`,
  ];
  const params: SqlParam[] = [
    { name: 'newStatus', type: 'STRING', value: newStatus },
    { name: 'urgency', type: 'STRING', value: String(urgency) },
  ];
  if (varianceQty !== null) {
    setClauses.push('VARIANCE_QTY = :varianceQty');
    params.push({ name: 'varianceQty', type: 'INT', value: String(varianceQty) });
  }
  if (args.proceed) {
    // Only a proceeding line gets a confirmed quantity — it's the approved
    // REQUESTED_QTY, unaltered. This tool never second-guesses the quantity
    // a PM already approved; PROCEED vs NEEDS_REVIEW is the only lever.
    setClauses.push('CONFIRMED_QTY = :confirmedQty');
    params.push({ name: 'confirmedQty', type: 'INT', value: String(asInt(requestedQty)) });
  }
  params.push({ name: 'lineKey', type: 'BIGINT', value: String(lineKey) });

  await runSql(
    `UPDATE ${FACT_RESTOCK_REQUEST} SET ${setClauses.join(', ')} WHERE RESTOCK_REQUEST_KEY = :lineKey`,
    params
  );

  if (args.note) {
    const clean = args.note.replace(/'/g, "''");
    await runSql(
      `UPDATE ${QUOTE_METADATA}
       SET decision_comments = CONCAT(
             COALESCE(decision_comments, ''),
             CASE WHEN decision_comments IS NULL OR decision_comments = '' THEN '' ELSE '\n\n' END,
             :note
           ),
           updated_at = current_timestamp()
       WHERE quote_id = :quoteId`,
      [
        { name: 'note', type: 'STRING', value: `[line ${lineKey} -> ${newStatus}] ${clean}` },
        { name: 'quoteId', type: 'STRING', value: String(quoteId) },
      ]
    );
  }

  return {
    restock_request_key: lineKey,
    transitioned: true,
    new_status: newStatus,
    current_stock_qty: currentStock,
    variance_qty: varianceQty,
    confirmed_qty: args.proceed ? asInt(requestedQty) : null,
  };
}

// ── MCP wiring ───────────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: 'persist_quote',
    description:
      'Save a completed restock quote to Delta as PENDING_APPROVAL. Writes one quote_metadata header row and one fact_restock_request line per candidate. Idempotent: the same candidate set on the same day returns the existing quote instead of creating a duplicate. Returns the quote_id, which is required by send_human_review.',
    inputSchema: {
      type: 'object',
      properties: {
        candidates_json: {
          type: 'string',
          description:
            'JSON array of the candidate objects from the Lakeflow scanner, each with item_id, warehouse_id, current_stock_qty, reorder_point_qty, suggested_reorder_qty and initial_urgency.',
        },
        summary_report: {
          type: 'string',
          description: 'The full consolidated Restock Quote text you produced, stored for the reviewer.',
        },
      },
      required: ['candidates_json', 'summary_report'],
    },
  },
  {
    name: 'send_human_review',
    description:
      'Notify the Production Manager in Microsoft Teams that a quote needs review, with a deep link to the Databricks Review App. Call only after persist_quote has returned a quote_id. Idempotent: does nothing if a card was already sent for this quote.',
    inputSchema: {
      type: 'object',
      properties: {
        quote_id: { type: 'string', description: 'quote_id returned by persist_quote.' },
        summary_report: { type: 'string', description: 'Quote text to show on the Teams card.' },
        review_url: {
          type: 'string',
          description: 'Deep link to the Review App for this quote, e.g. https://<workspace>/apps/restock-review?quote_id=<id>',
        },
      },
      required: ['quote_id', 'summary_report', 'review_url'],
    },
  },
  {
    name: 'fulfill_restock_request',
    description:
      'Record your PROCEED / NEEDS_REVIEW verdict on a single APPROVED restock line. This tool computes the current stock, variance vs the quote-time stock, and the confirmed quantity itself from live data — you do not supply any of those numbers. Your only input is the judgment call: proceed=true moves the line to FULFILLING at its originally-approved quantity; proceed=false moves it to NEEDS_REVIEW instead, for cases like the request sitting a long time before approval and the stock situation having materially changed since — already replenished, already covered by a newer PO, demand having collapsed, etc. Idempotent: only acts on a line that is currently APPROVED.',
    inputSchema: {
      type: 'object',
      properties: {
        restock_request_key: {
          type: 'integer',
          description: 'RESTOCK_REQUEST_KEY of the single part-line being decided.',
        },
        proceed: {
          type: 'boolean',
          description:
            'true = still makes sense, move to FULFILLING at the approved quantity. false = flag NEEDS_REVIEW instead of writing the transition blindly.',
        },
        note: {
          type: 'string',
          description: 'One or two sentences explaining the verdict, appended to the quote for the PM to see.',
        },
      },
      required: ['restock_request_key', 'proceed'],
    },
  },
];

const HANDLERS: Record<string, (args: Record<string, never>) => Promise<unknown>> = {
  persist_quote: persistQuote as never,
  send_human_review: sendHumanReview as never,
  fulfill_restock_request: fulfillRestockRequest as never,
};

/** JSON-RPC 2.0 / MCP handler. Mounted at POST /api/mcp. */
export async function handleMcpRequest(req: Request, res: Response) {
  const { id, method, params } = (req.body ?? {}) as {
    id?: string | number;
    method?: string;
    params?: { name?: string; arguments?: Record<string, never> };
  };

  const reply = (result: unknown) => res.json({ jsonrpc: '2.0', id: id ?? null, result });
  const fail = (code: number, message: string) =>
    res.json({ jsonrpc: '2.0', id: id ?? null, error: { code, message } });

  try {
    switch (method) {
      case 'initialize':
        return reply({
          protocolVersion: '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'restockify-actions', version: '1.0.0' },
        });

      case 'notifications/initialized':
        return res.status(202).end();

      case 'ping':
        return reply({});

      case 'tools/list':
        return reply({ tools: TOOLS });

      case 'tools/call': {
        const toolName = params?.name ?? '';
        const handler = HANDLERS[toolName];
        if (!handler) return fail(-32602, `Unknown tool: ${toolName}`);
        try {
          const result = await handler(params?.arguments ?? ({} as Record<string, never>));
          return reply({ content: [{ type: 'text', text: JSON.stringify(result) }] });
        } catch (err) {
          // Tool-level failures are reported as MCP results with isError so the
          // agent can read and reason about them, per the MCP spec.
          const message = err instanceof Error ? err.message : String(err);
          console.error(`MCP tool ${toolName} failed:`, err);
          return reply({ content: [{ type: 'text', text: `Error: ${message}` }], isError: true });
        }
      }

      default:
        return fail(-32601, `Method not found: ${method}`);
    }
  } catch (err) {
    console.error('MCP request failed', err);
    return fail(-32603, err instanceof Error ? err.message : 'Internal error');
  }
}
