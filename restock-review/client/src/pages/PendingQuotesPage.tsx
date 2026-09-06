import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
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
  const { data, loading, error } = useAnalyticsQuery('pending_quotes', {});

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
                  return (
                    <TableRow key={q.quote_id}>
                      <TableCell>
                        <Link to={`/quotes/${q.quote_id}`} className="text-primary underline underline-offset-4 hover:text-primary/80 font-medium">
                          {q.quote_id}
                        </Link>
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
