import { useEffect, useState } from 'react';
import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
import { clearSubmitted, submittedQuoteIds } from '../lib/pendingDecisions';
import {
  Badge,
  Card,
  CardContent,
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
import { Link } from 'react-router';

/** Indian scale, because the figures span lakh and crore and a raw rupee total is unreadable. */
function formatCrore(value: number | string | null): string {
  const n = Number(value);
  if (!n || Number.isNaN(n)) return '—';
  if (n >= 1e7) return `Rs ${(n / 1e7).toFixed(2)} cr`;
  if (n >= 1e5) return `Rs ${(n / 1e5).toFixed(2)} L`;
  return `Rs ${Math.round(n).toLocaleString('en-IN')}`;
}

const URGENCY_RANK_LABEL: Record<number, string> = {
  1: 'CRITICAL',
  2: 'HIGH',
  3: 'MEDIUM',
  4: 'LOW',
};

const URGENCY_BADGE_VARIANT: Record<string, 'destructive' | 'secondary' | 'outline'> = {
  CRITICAL: 'destructive',
  HIGH: 'destructive',
  MEDIUM: 'secondary',
  LOW: 'outline',
};

export function PendingQuotesPage() {
  // Remounted when a submitted quote finishes applying, so the row disappears on its own
  // instead of leaving the reader to guess whether to press submit again.
  const [refreshKey, setRefreshKey] = useState(0);
  return <PendingQuotesList key={refreshKey} onApplied={() => setRefreshKey((k) => k + 1)} />;
}

function PendingQuotesList({ onApplied }: { onApplied: () => void }) {
  const { data, loading, error } = useAnalyticsQuery('pending_quotes', {});
  const [applying, setApplying] = useState<Set<string>>(() => submittedQuoteIds());

  // Watch the quotes we know were submitted from this tab. The job takes one to two and a half
  // minutes and writes nothing until it finishes, so without this the row sits there looking
  // untouched -- which is what got one decision submitted twice.
  useEffect(() => {
    if (applying.size === 0) return;
    let cancelled = false;

    const timer = setInterval(() => {
      if (cancelled) return;
      const stillListed = new Set((data ?? []).map((q) => q.quote_id));
      let changed = false;
      for (const quoteId of applying) {
        // Gone from the pending list means the job wrote its decisions.
        if (!stillListed.has(quoteId)) {
          clearSubmitted(quoteId);
          changed = true;
        }
      }
      const current = submittedQuoteIds();
      if (changed || current.size !== applying.size) {
        setApplying(current);
        onApplied();
      }
    }, 5000);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [applying, data, onApplied]);

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-foreground">Waiting on you</h2>
        <p className="text-sm text-muted-foreground">
          {/* The old copy named the fulfillment guardrail, which was retired with the decision
              restructure, and spelled statuses in database casing at a planner. */}
          Each of these has actions nobody has decided yet. Open one to see what it found and why.
        </p>
      </div>

      <Card className="shadow-lg">
        <CardContent>
          {loading && (
            <div className="space-y-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          )}

          {error && (
            <div className="text-destructive bg-destructive/10 p-3 rounded-md text-sm">
              Failed to load pending quotes: {error}
            </div>
          )}

          {data && data.length === 0 && (
            <Empty>
              <EmptyHeader>
                <EmptyTitle>Nothing to review</EmptyTitle>
                <EmptyDescription>
                  No quote currently has a line in PENDING_APPROVAL or NEEDS_REVIEW.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}

          {data && data.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Quote</TableHead>
                  <TableHead>Urgency</TableHead>
                  <TableHead className="text-right">At stake</TableHead>
                  <TableHead className="text-right">To decide</TableHead>
                  <TableHead>What it found</TableHead>
                  <TableHead>Raised</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((q) => {
                  const urgencyLabel = URGENCY_RANK_LABEL[q.top_urgency_rank] ?? 'UNKNOWN';
                  const isApplying = applying.has(q.quote_id);
                  return (
                    <TableRow key={q.quote_id} className={isApplying ? 'opacity-60' : undefined}>
                      <TableCell>
                        <Link to={`/quotes/${q.quote_id}`} className="text-primary underline underline-offset-4 hover:text-primary/80 font-medium">
                          {q.quote_id}
                        </Link>
                        {isApplying && (
                          <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                            Applying your decisions — this can take a couple of minutes
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant={URGENCY_BADGE_VARIANT[urgencyLabel] ?? 'outline'}>{urgencyLabel}</Badge>
                      </TableCell>
                      {/* Money first. Three of the four count columns this replaces were zero on
                          every row, and none of them said which quote was worth opening. */}
                      <TableCell className="text-right font-medium tabular-nums">
                        {formatCrore(q.total_exposure)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {q.pending_lines} of {q.total_lines}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {(q.finding_types ?? '').split(', ').filter(Boolean).map((f) => (
                            <Badge key={f} variant="secondary" className="text-[10px] font-normal">
                              {f.replace(/_/g, ' ').toLowerCase()}
                            </Badge>
                          ))}
                        </div>
                      </TableCell>
                      <TableCell className="text-muted-foreground text-sm">
                        {q.created_at ? new Date(q.created_at).toLocaleString() : '—'}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
