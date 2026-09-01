import { useState } from 'react';
import { Link } from 'react-router';
import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
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

type LineState = { status: 'idle' | 'submitting' | 'error'; message?: string };

export function FulfillingOrdersPage() {
  // Bumping this remounts the query hook below, forcing a fresh fetch after a
  // line is marked COMPLETED — useAnalyticsQuery has no refetch() itself.
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-foreground">Fulfilling Orders</h2>
        <p className="text-sm text-muted-foreground">
          Part-lines confirmed and moving through fulfillment. Mark a line Completed once the stock has physically
          arrived.
        </p>
      </div>

      <FulfillingLinesCard key={refreshKey} onCompleted={() => setRefreshKey((k) => k + 1)} />
    </div>
  );
}

function FulfillingLinesCard({ onCompleted }: { onCompleted: () => void }) {
  const { data, loading, error } = useAnalyticsQuery('fulfilling_lines', {});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [lineStates, setLineStates] = useState<Record<number, LineState>>({});

  async function complete(lineKey: number) {
    setLineStates((s) => ({ ...s, [lineKey]: { status: 'submitting' } }));
    try {
      const res = await fetch(`/api/lines/${lineKey}/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: notes[lineKey]?.trim() || undefined }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.error || `Request failed (${res.status})`);
      }
      onCompleted();
    } catch (err) {
      setLineStates((s) => ({
        ...s,
        [lineKey]: { status: 'error', message: err instanceof Error ? err.message : 'Failed to mark completed' },
      }));
    }
  }

  return (
    <Card className="shadow-lg">
      <CardHeader>
        <CardTitle>Lines Awaiting Receipt Confirmation</CardTitle>
        <CardDescription>Approved lines the Supervisor has already re-validated and moved to FULFILLING.</CardDescription>
      </CardHeader>
      <CardContent>
        {loading && (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        )}
        {error && (
          <div className="text-destructive bg-destructive/10 p-3 rounded-md text-sm">
            Failed to load fulfilling lines: {error}
          </div>
        )}
        {data && data.length === 0 && (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>Nothing fulfilling right now</EmptyTitle>
              <EmptyDescription>No fact_restock_request line currently has status FULFILLING.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {data && data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Quote</TableHead>
                <TableHead>Part</TableHead>
                <TableHead>Warehouse</TableHead>
                <TableHead>Urgency</TableHead>
                <TableHead className="text-right">Requested / Confirmed</TableHead>
                <TableHead>Note</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((line) => {
                const state = lineStates[line.RESTOCK_REQUEST_KEY] ?? { status: 'idle' as const };
                return (
                  <TableRow key={line.RESTOCK_REQUEST_KEY}>
                    <TableCell>
                      <Link
                        to={`/quotes/${encodeURIComponent(line.QUOTE_ID)}`}
                        className="text-primary underline underline-offset-4 hover:text-primary/80"
                      >
                        {line.QUOTE_ID}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <div className="font-medium">{line.PART_ID}</div>
                      <div className="text-xs text-muted-foreground">{line.PART_NAME}</div>
                    </TableCell>
                    <TableCell>{line.WAREHOUSE_ID}</TableCell>
                    <TableCell>
                      <Badge variant={URGENCY_BADGE_VARIANT[line.URGENCY_LEVEL] ?? 'outline'}>{line.URGENCY_LEVEL}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {line.REQUESTED_QTY} / {line.CONFIRMED_QTY ?? '—'}
                    </TableCell>
                    <TableCell className="min-w-[220px]">
                      <Textarea
                        className="min-h-[36px] text-xs"
                        placeholder="Add a receiving note (optional)…"
                        value={notes[line.RESTOCK_REQUEST_KEY] ?? ''}
                        disabled={state.status === 'submitting'}
                        onChange={(e) => setNotes((n) => ({ ...n, [line.RESTOCK_REQUEST_KEY]: e.target.value }))}
                      />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-col items-end gap-1">
                        <Button
                          size="sm"
                          disabled={state.status === 'submitting'}
                          onClick={() => complete(line.RESTOCK_REQUEST_KEY)}
                        >
                          {state.status === 'submitting' ? 'Completing…' : 'Mark Completed'}
                        </Button>
                        {state.status === 'error' && (
                          <Alert variant="destructive" className="mt-1 max-w-xs">
                            <AlertDescription className="text-xs">{state.message}</AlertDescription>
                          </Alert>
                        )}
                      </div>
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
