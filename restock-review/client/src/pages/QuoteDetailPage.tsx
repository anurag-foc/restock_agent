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

type LineDecisionState = { status: 'idle' | 'submitting' | 'error'; message?: string };

type DecisionResult = {
  decision: 'APPROVED' | 'REJECTED';
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
            {lastDecision.decision === 'APPROVED' ? (
              <>
                Approval submitted (run #{lastDecision.decisionRunId}). The line is being set to
                APPROVED, then re-validated against live stock before moving to FULFILLING —
                refresh in a moment to see the result.
              </>
            ) : (
              <>
                Rejection submitted (run #{lastDecision.decisionRunId}). Refresh in a moment to
                see the line marked REJECTED.
              </>
            )}
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
  const [lineStates, setLineStates] = useState<Record<number, LineDecisionState>>({});

  async function decide(lineKey: number, decision: 'APPROVED' | 'REJECTED') {
    setLineStates((s) => ({ ...s, [lineKey]: { status: 'submitting' } }));
    try {
      const res = await fetch(`/api/quotes/${encodeURIComponent(quoteId)}/lines/${lineKey}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.error || `Request failed (${res.status})`);
      }
      onDecided({ decision, decisionRunId: body.decisionRunId });
    } catch (err) {
      setLineStates((s) => ({
        ...s,
        [lineKey]: { status: 'error', message: err instanceof Error ? err.message : 'Failed to save decision' },
      }));
    }
  }

  return (
    <Card className="shadow-lg">
      <CardHeader>
        <CardTitle>Part Lines</CardTitle>
        <CardDescription>Each line can be approved or rejected independently.</CardDescription>
      </CardHeader>
      <CardContent>
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
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Part</TableHead>
                <TableHead>Warehouse</TableHead>
                <TableHead className="text-right">Stock / Reorder</TableHead>
                <TableHead className="text-right">Requested Qty</TableHead>
                <TableHead>Urgency</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((line) => {
                const state = lineStates[line.RESTOCK_REQUEST_KEY] ?? { status: 'idle' as const };
                const isPending = line.REQUEST_STATUS === 'PENDING_APPROVAL';
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
                    <TableCell className="text-right">
                      {isPending ? (
                        <div className="flex flex-col items-end gap-1">
                          <div className="flex gap-2 justify-end">
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={state.status === 'submitting'}
                              onClick={() => decide(line.RESTOCK_REQUEST_KEY, 'REJECTED')}
                            >
                              Reject
                            </Button>
                            <Button
                              size="sm"
                              disabled={state.status === 'submitting'}
                              onClick={() => decide(line.RESTOCK_REQUEST_KEY, 'APPROVED')}
                            >
                              Approve
                            </Button>
                          </div>
                          {state.status === 'error' && (
                            <Alert variant="destructive" className="mt-1 max-w-xs">
                              <AlertDescription className="text-xs">{state.message}</AlertDescription>
                            </Alert>
                          )}
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
        )}
      </CardContent>
    </Card>
  );
}
