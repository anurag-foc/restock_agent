import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
import {
  Badge,
  Card,
  CardContent,
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
import { Link } from 'react-router';
import { URGENCY_BADGE_CLASS, DEFAULT_BADGE_CLASS } from '../lib/badge-colors';

const URGENCY_RANK_LABEL: Record<number, string> = {
  1: 'CRITICAL',
  2: 'HIGH',
  3: 'MEDIUM',
  4: 'LOW',
};

export function PendingQuotesPage() {
  const { data, loading, error } = useAnalyticsQuery('pending_quotes', {});

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-foreground">Quotes Awaiting Your Approval</h2>
        <p className="text-sm text-muted-foreground">
          Quotes with at least one part still waiting on your decision, or flagged for a second look after
          fulfillment re-checked it. Approve or reject each part individually.
        </p>
      </div>

      <Card className="shadow-lg">
        <CardHeader>
          <CardTitle>Quotes Needing Action</CardTitle>
        </CardHeader>
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
                  <TableHead>Top Urgency</TableHead>
                  <TableHead className="text-center">Pending</TableHead>
                  <TableHead className="text-center">Needs Review</TableHead>
                  <TableHead className="text-center">Approved</TableHead>
                  <TableHead className="text-center">Rejected</TableHead>
                  <TableHead className="text-center">Total Lines</TableHead>
                  <TableHead>Created</TableHead>
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
                        <Badge className={URGENCY_BADGE_CLASS[urgencyLabel] ?? DEFAULT_BADGE_CLASS}>{urgencyLabel}</Badge>
                      </TableCell>
                      <TableCell className="text-center">{q.pending_lines}</TableCell>
                      <TableCell className="text-center">{q.needs_review_lines}</TableCell>
                      <TableCell className="text-center">{q.approved_lines}</TableCell>
                      <TableCell className="text-center">{q.rejected_lines}</TableCell>
                      <TableCell className="text-center">{q.total_lines}</TableCell>
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
