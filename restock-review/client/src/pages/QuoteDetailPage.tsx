import { useState } from 'react';
import { useParams, Link } from 'react-router';
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

export function QuoteDetailPage() {
  const { quoteId = '' } = useParams();
  // Bumping this remounts the two query hooks below, forcing a fresh fetch
  // after a decision is written — useAnalyticsQuery has no refetch() itself.
  const [refreshKey, setRefreshKey] = useState(0);
  const [lastDecision, setLastDecision] = useState<DecisionResult | null>(null);

  function handleDecided(result: DecisionResult) {
    setLastDecision(result);
    setRefreshKey((k) => k + 1);
  }

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link to="/" className="text-sm text-primary underline underline-offset-4 hover:text-primary/80">
            ← All pending quotes
          </Link>
          <h2 className="text-2xl font-bold text-foreground mt-1">{quoteId}</h2>
        </div>
      </div>

      {lastDecision && (
        <Alert>
          <AlertDescription>
            Submitted {lastDecision.lines.length} decision{lastDecision.lines.length === 1 ? '' : 's'} (run #
            {lastDecision.decisionRunId}):{' '}
            {lastDecision.lines.map((l) => `line ${l.lineKey} → ${l.decision}`).join(', ')}. Approved lines are
            re-validated against live stock before moving to FULFILLING — refresh in a moment to see the result.
          </AlertDescription>
        </Alert>
      )}

      <QuoteHeaderCard quoteId={quoteId} key={`header-${refreshKey}`} />
      <QuoteLinesCard quoteId={quoteId} key={`lines-${refreshKey}`} onDecided={handleDecided} />
    </div>
  );
}

function QuoteHeaderCard({ quoteId }: { quoteId: string }) {
  const { data, loading, error } = useAnalyticsQuery('quote_header', {
    quoteId: sql.string(quoteId),
  });

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
            <pre className="whitespace-pre-wrap text-sm font-mono bg-muted/50 rounded-md p-4">
              {data[0].summary_report}
            </pre>
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

function QuoteLinesCard({ quoteId, onDecided }: { quoteId: string; onDecided: (result: DecisionResult) => void }) {
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
        decision: d[lineKey]?.decision === decision ? null : decision,
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
          Mark each line Approved or Rejected and add a note if useful, then submit all decisions together.
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
                  <TableHead className="text-right">Stock / Reorder</TableHead>
                  <TableHead className="text-right">Requested Qty</TableHead>
                  <TableHead>Urgency</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Note</TableHead>
                  <TableHead className="text-right">Decision</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((line) => {
                  const isPending = line.REQUEST_STATUS === 'PENDING_APPROVAL';
                  const draft = drafts[line.RESTOCK_REQUEST_KEY] ?? { decision: null, note: '' };
                  return (
                    <TableRow key={line.RESTOCK_REQUEST_KEY}>
                      <TableCell>
                        <div className="font-medium">{line.PART_ID}</div>
                        <div className="text-xs text-muted-foreground">{line.PART_NAME}</div>
                      </TableCell>
                      <TableCell>{line.WAREHOUSE_ID}</TableCell>
                      <TableCell className="text-right">
                        {line.CURRENT_STOCK_QTY} / {line.REORDER_POINT_QTY}
                      </TableCell>
                      <TableCell className="text-right">{line.REQUESTED_QTY}</TableCell>
                      <TableCell>
                        <Badge variant={URGENCY_BADGE_VARIANT[line.URGENCY_LEVEL] ?? 'outline'}>{line.URGENCY_LEVEL}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={STATUS_BADGE_VARIANT[line.REQUEST_STATUS] ?? 'outline'}>{line.REQUEST_STATUS}</Badge>
                      </TableCell>
                      <TableCell className="min-w-[220px]">
                        {isPending ? (
                          <Textarea
                            className="min-h-[36px] text-xs"
                            placeholder="Add a note for the agent to reason with (optional)…"
                            value={draft.note}
                            disabled={submitState.status === 'submitting'}
                            onChange={(e) => setDraftNote(line.RESTOCK_REQUEST_KEY, e.target.value)}
                          />
                        ) : (
                          <span className="text-xs text-muted-foreground">{line.NOTE || '—'}</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {isPending ? (
                          <div className="flex gap-2 justify-end">
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
                              variant={draft.decision === 'APPROVED' ? 'default' : 'outline'}
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
