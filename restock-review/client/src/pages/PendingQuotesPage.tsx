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
        <h2 className="text-2xl font-bold text-foreground">Restock Quotes Awaiting Review</h2>
        <p className="text-sm text-muted-foreground">
          Quotes with at least one part-line still in PENDING_APPROVAL. Approve or reject each line individually.
        </p>
      </div>

      <Card className="shadow-lg">
        <CardHeader>
          <CardTitle>Pending Quotes</CardTitle>
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
                  No quote currently has a line in PENDING_APPROVAL.
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
                  <TableHead className="text-right">Pending</TableHead>
                  <TableHead className="text-right">Approved</TableHead>
                  <TableHead className="text-right">Rejected</TableHead>
                  <TableHead className="text-right">Total Lines</TableHead>
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
                        <Badge variant={URGENCY_BADGE_VARIANT[urgencyLabel] ?? 'outline'}>{urgencyLabel}</Badge>
                      </TableCell>
                      <TableCell className="text-right">{q.pending_lines}</TableCell>
                      <TableCell className="text-right">{q.approved_lines}</TableCell>
                      <TableCell className="text-right">{q.rejected_lines}</TableCell>
                      <TableCell className="text-right">{q.total_lines}</TableCell>
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
