import { useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
import { sql } from '@databricks/appkit-ui/js';
import {
  Alert,
  AlertDescription,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyDescription,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Textarea,
} from '@databricks/appkit-ui/react';
import { URGENCY_BADGE_CLASS, STATUS_BADGE_CLASS, DEFAULT_BADGE_CLASS } from '../lib/badge-colors';
import { IntelligenceReport } from '../components/IntelligenceReport';

const URGENCY_BADGE_VARIANT: Record<string, 'destructive' | 'secondary' | 'outline'> = {
  CRITICAL: 'destructive',
  HIGH: 'destructive',
  MEDIUM: 'secondary',
  LOW: 'outline',
};

const STATUS_BADGE_VARIANT: Record<string, 'default' | 'destructive' | 'secondary' | 'outline'> = {
  PENDING_APPROVAL: 'secondary',
  APPROVED: 'default',
  REJECTED: 'destructive',
  FULFILLING: 'default',
  COMPLETED: 'default',
  NEEDS_REVIEW: 'outline',
};

type Draft = { decision: 'APPROVED' | 'REJECTED' | null; note: string };

type SubmitState = { status: 'idle' | 'submitting' | 'error'; message?: string };

type DecisionResult = {
  lines: Array<{ lineKey: number; decision: 'APPROVED' | 'REJECTED' }>;
  decisionRunId?: number;
};

// Databricks Jobs API life_cycle_state values that mean the run is done,
// one way or another -- see /api/jobs/:jobKey/runs/:runId (run-detail route
// registered by AppKit's jobs() plugin).
const TERMINAL_LIFE_CYCLE_STATES = new Set(['TERMINATED', 'SKIPPED', 'INTERNAL_ERROR']);
const POLL_INTERVAL_MS = 5000;
// invoke_fulfillment runs one Supervisor conversation per approved line,
// sequentially, each ~80-110s -- a batch of several approved lines can take
// several minutes, so poll for up to 15 minutes before giving up.
const MAX_POLL_ATTEMPTS = 180;

export function QuoteDetailPage() {
  const { quoteId = '' } = useParams();
  // Bumping this remounts the two query hooks below, forcing a fresh fetch
  // after a decision run finishes — useAnalyticsQuery has no refetch() itself.
  const [refreshKey, setRefreshKey] = useState(0);
  const [lastDecision, setLastDecision] = useState<DecisionResult | null>(null);
  const [runState, setRunState] = useState<{ status: 'polling' | 'done' | 'timeout' | 'error'; resultState?: string }>();
  const [decisionComments, setDecisionComments] = useState<string | null>(null);
  const pollAbortRef = useRef(false);

  useEffect(() => {
    return () => {
      pollAbortRef.current = true;
    };
  }, []);

  async function handleDecided(result: DecisionResult) {
    setLastDecision(result);
    if (!result.decisionRunId) return;

    pollAbortRef.current = false;
    setRunState({ status: 'polling' });
    for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt++) {
      if (pollAbortRef.current) return;
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      if (pollAbortRef.current) return;
      try {
        const res = await fetch(`/api/jobs/restock_decision/runs/${result.decisionRunId}`);
        if (!res.ok) continue;
        const run = await res.json();
        const lifeCycleState = run?.state?.life_cycle_state;
        if (TERMINAL_LIFE_CYCLE_STATES.has(lifeCycleState)) {
          setRunState({ status: 'done', resultState: run?.state?.result_state });
          setRefreshKey((k) => k + 1);
          return;
        }
      } catch {
        // transient fetch error while polling -- keep trying until MAX_POLL_ATTEMPTS
      }
    }
    setRunState({ status: 'timeout' });
  }

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Button asChild variant="ghost" size="sm" className="-ml-2.5 gap-1.5 text-muted-foreground hover:text-foreground">
            <Link to="/">
              <ArrowLeft className="size-4" />
              Back to Quotes
            </Link>
          </Button>
          <h2 className="text-2xl font-bold text-foreground mt-1">{quoteId}</h2>
        </div>
      </div>

      {lastDecision && (
        <Alert>
          <AlertDescription>
            Submitted {lastDecision.lines.length} decision{lastDecision.lines.length === 1 ? '' : 's'} (run #
            {lastDecision.decisionRunId}): {lastDecision.lines.map((l) => `line ${l.lineKey} → ${l.decision}`).join(', ')}.
            {runState?.status === 'polling' &&
              ' Applying decisions and re-validating any approved lines against live stock — this page will refresh automatically when it finishes.'}
            {runState?.status === 'done' &&
              ` Done (${runState.resultState ?? 'unknown result'}) — this page has refreshed with the latest status.`}
            {runState?.status === 'timeout' &&
              ' Still running after several minutes — refresh the page in a bit to see the result.'}
          </AlertDescription>
        </Alert>
      )}

      <QuoteHeaderCard quoteId={quoteId} key={`header-${refreshKey}`} onLoaded={setDecisionComments} />
      <QuoteLinesCard
        quoteId={quoteId}
        key={`lines-${refreshKey}`}
        onDecided={handleDecided}
        decisionComments={decisionComments}
      />
    </div>
  );
}

// Extracts the most recent guardrail reasoning for one line out of
// quote_metadata.decision_comments, which fulfill_restock_request appends to
// as "[line <key> -> <status>] <reason>" blocks separated by blank lines (see
// mcp-inventory-actions/server/tools.py) -- there is no per-line column for
// this, so the quote-level blob is the only place it lives.
function extractLineReasoning(decisionComments: string | null, lineKey: number): string | null {
  if (!decisionComments) return null;
  const prefix = `[line ${lineKey} ->`;
  const blocks = decisionComments.split('\n\n').filter((b) => b.startsWith(prefix));
  return blocks.length > 0 ? blocks[blocks.length - 1] : null;
}

function QuoteHeaderCard({ quoteId, onLoaded }: { quoteId: string; onLoaded: (decisionComments: string | null) => void }) {
  const { data, loading, error } = useAnalyticsQuery('quote_header', {
    quoteId: sql.string(quoteId),
  });

  useEffect(() => {
    if (data && data.length > 0) {
      onLoaded(data[0].decision_comments ?? null);
    }
    // onLoaded is a setState function from the parent -- stable identity, safe to omit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  return (
    <Card className="shadow-lg">
      <CardHeader>
        <CardTitle>Intelligence Summary</CardTitle>
        <CardDescription>Genie Agent's reasoning report for this quote</CardDescription>
      </CardHeader>
      <CardContent>
        {loading && (
          <div className="space-y-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        )}
        {error && <div className="text-destructive bg-destructive/10 p-3 rounded-md text-sm">Failed to load quote: {error}</div>}
        {data && data.length === 0 && (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>Quote not found</EmptyTitle>
              <EmptyDescription>No quote_metadata row exists for {quoteId}.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {data && data.length > 0 && (
          <div className="space-y-3">
            <IntelligenceReport text={data[0].summary_report} />
            <div className="text-xs text-muted-foreground">
              Created by {data[0].created_by} · {data[0].created_at ? new Date(data[0].created_at).toLocaleString() : '—'}
              {data[0].teams_sent_at && <> · Teams card sent {new Date(data[0].teams_sent_at).toLocaleString()}</>}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function QuoteLinesCard({
  quoteId,
  onDecided,
  decisionComments,
}: {
  quoteId: string;
  onDecided: (result: DecisionResult) => void;
  decisionComments: string | null;
}) {
  const { data, loading, error } = useAnalyticsQuery('quote_lines', {
    quoteId: sql.string(quoteId),
  });
  // Approve/Reject on a line only stages a local draft (decision + note) --
  // nothing is written until Final Submit sends every staged line as one
  // batch to the restock_decision job (see server.ts POST /decisions).
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [submitState, setSubmitState] = useState<SubmitState>({ status: 'idle' });

  function setDraftDecision(lineKey: number, decision: 'APPROVED' | 'REJECTED') {
    setDrafts((d) => ({
      ...d,
      [lineKey]: {
        note: d[lineKey]?.note ?? '',
        decision,
      },
    }));
  }

  function setDraftNote(lineKey: number, note: string) {
    setDrafts((d) => ({ ...d, [lineKey]: { decision: d[lineKey]?.decision ?? null, note } }));
  }

  const stagedLines = Object.entries(drafts)
    .filter(([, d]) => d.decision !== null)
    .map(([lineKey, d]) => ({ lineKey: Number(lineKey), decision: d.decision as 'APPROVED' | 'REJECTED', note: d.note }));

  async function submitAll() {
    setSubmitState({ status: 'submitting' });
    try {
      const res = await fetch(`/api/quotes/${encodeURIComponent(quoteId)}/decisions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decisions: stagedLines }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.error || `Request failed (${res.status})`);
      }
      setSubmitState({ status: 'idle' });
      setDrafts({});
      onDecided({ lines: body.lines, decisionRunId: body.decisionRunId });
    } catch (err) {
      setSubmitState({ status: 'error', message: err instanceof Error ? err.message : 'Failed to submit decisions' });
    }
  }

  return (
    <Card className="shadow-lg">
      <CardHeader>
        <CardTitle>Part Lines</CardTitle>
        <CardDescription>
          Mark each line Approved or Rejected and add a note if useful, then submit all decisions together. A line
          flagged NEEDS_REVIEW can be decided again — Approve retries fulfillment, Reject cancels it.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        )}
        {error && <div className="text-destructive bg-destructive/10 p-3 rounded-md text-sm">Failed to load lines: {error}</div>}
        {data && data.length === 0 && (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>No lines found</EmptyTitle>
              <EmptyDescription>No fact_restock_request rows for {quoteId}.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {data && data.length > 0 && (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Part</TableHead>
                  <TableHead>Warehouse</TableHead>
                  <TableHead className="text-center">Stock / Reorder</TableHead>
                  <TableHead className="text-center">Requested Qty</TableHead>
                  <TableHead>Urgency</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Note</TableHead>
                  <TableHead className="text-center">Decision</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((line) => {
                  const isActionable = line.REQUEST_STATUS === 'PENDING_APPROVAL' || line.REQUEST_STATUS === 'NEEDS_REVIEW';
                  const draft = drafts[line.RESTOCK_REQUEST_KEY] ?? { decision: null, note: '' };
                  const reasoning =
                    line.REQUEST_STATUS === 'NEEDS_REVIEW'
                      ? extractLineReasoning(decisionComments, line.RESTOCK_REQUEST_KEY)
                      : null;
                  return (
                    <TableRow key={line.RESTOCK_REQUEST_KEY}>
                      <TableCell>
                        <div className="font-medium">{line.PART_ID}</div>
                        <div className="text-xs text-muted-foreground">{line.PART_NAME}</div>
                      </TableCell>
                      <TableCell>{line.WAREHOUSE_ID}</TableCell>
                      <TableCell className="text-center">
                        {line.CURRENT_STOCK_QTY} / {line.REORDER_POINT_QTY}
                      </TableCell>
                      <TableCell className="text-center">{line.REQUESTED_QTY}</TableCell>
                      <TableCell>
                        <Badge className={URGENCY_BADGE_CLASS[line.URGENCY_LEVEL] ?? DEFAULT_BADGE_CLASS}>{line.URGENCY_LEVEL}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge className={STATUS_BADGE_CLASS[line.REQUEST_STATUS] ?? DEFAULT_BADGE_CLASS}>{line.REQUEST_STATUS}</Badge>
                        {reasoning && (
                          <div className="text-xs text-muted-foreground mt-1 w-[220px] whitespace-normal">
                            {reasoning.replace(/^\[line \d+ -> [A-Z_]+\]\s*/, '')}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="min-w-[220px]">
                        {isActionable ? (
                          <Textarea
                            className="min-h-[36px] text-xs max-w-[220px] border-transparent shadow-none resize-none hover:border-input focus-visible:border-ring focus-visible:shadow-xs transition-colors"
                            placeholder="Add a note for the agent to reason with (optional)…"
                            value={draft.note}
                            disabled={submitState.status === 'submitting'}
                            onChange={(e) => setDraftNote(line.RESTOCK_REQUEST_KEY, e.target.value)}
                          />
                        ) : (
                          <span className="text-xs text-muted-foreground">{line.NOTE || '—'}</span>
                        )}
                      </TableCell>
                      <TableCell className="text-center">
                        {isActionable ? (
                          <div className="flex gap-2 justify-center">
                            <Button
                              size="sm"
                              variant={draft.decision === 'REJECTED' ? 'destructive' : 'outline'}
                              disabled={submitState.status === 'submitting'}
                              onClick={() => setDraftDecision(line.RESTOCK_REQUEST_KEY, 'REJECTED')}
                            >
                              Reject
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              className={draft.decision === 'APPROVED' ? 'border-transparent bg-emerald-500 text-white hover:bg-emerald-600 hover:text-white' : ''}
                              disabled={submitState.status === 'submitting'}
                              onClick={() => setDraftDecision(line.RESTOCK_REQUEST_KEY, 'APPROVED')}
                            >
                              Approve
                            </Button>
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            {line.DECISION ? `Decided: ${line.DECISION}` : '—'}
                          </span>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>

            <div className="flex items-center justify-end gap-3 border-t pt-4">
              {submitState.status === 'error' && (
                <Alert variant="destructive" className="flex-1">
                  <AlertDescription className="text-xs">{submitState.message}</AlertDescription>
                </Alert>
              )}
              <span className="text-xs text-muted-foreground">
                {stagedLines.length} line{stagedLines.length === 1 ? '' : 's'} staged
              </span>
              <Button disabled={stagedLines.length === 0 || submitState.status === 'submitting'} onClick={submitAll}>
                {submitState.status === 'submitting' ? 'Submitting…' : 'Final Submit'}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
