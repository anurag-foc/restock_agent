import { useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router';
import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
import { sql } from '@databricks/appkit-ui/js';
import {
  Alert,
  AlertDescription,
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
} from '@databricks/appkit-ui/react';
import { DecisionBoard } from '../components/DecisionBoard';
import type { Draft, ReasonCode } from '../components/DecisionBoard';

// Required on a rejection, because it decides whether the finding ever comes back. Not a
// bureaucratic field: "can't act right now" is a snooze that returns in two weeks, while "not a
// real problem" closes it until the amount at stake materially grows. Free text cannot carry
// that -- "no" and "not now" read the same and mean opposite things -- and getting it wrong
// teaches the system to bury hard problems, which is the failure this product exists to fix.

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
// The decision job is now a single deterministic task -- no Supervisor turn per approved line
// -- so it finishes in seconds rather than the several minutes the old fulfillment step took.
// The generous ceiling is kept anyway: it costs nothing on a fast run, and a warehouse under
// load is the wrong moment to discover the timeout was tuned to the happy path.
const MAX_POLL_ATTEMPTS = 180;

export function QuoteDetailPage() {
  const { quoteId = '' } = useParams();
  // Bumping this remounts the two query hooks below, forcing a fresh fetch
  // after a decision run finishes — useAnalyticsQuery has no refetch() itself.
  const [refreshKey, setRefreshKey] = useState(0);
  const [lastDecision, setLastDecision] = useState<DecisionResult | null>(null);
  const [runState, setRunState] = useState<{ status: 'polling' | 'done' | 'timeout' | 'error'; resultState?: string }>();
  const [summaryReport, setSummaryReport] = useState<string | null>(null);
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

      <QuoteHeaderCard
        quoteId={quoteId}
        key={`header-${refreshKey}`}
        onLoaded={({ summaryReport: report }) => setSummaryReport(report)}
      />
      <QuoteLinesCard
        quoteId={quoteId}
        key={`lines-${refreshKey}`}
        onDecided={handleDecided}
        summaryReport={summaryReport}
      />
    </div>
  );
}


function QuoteHeaderCard({ quoteId, onLoaded }: { quoteId: string; onLoaded: (row: { summaryReport: string | null; decisionComments: string | null }) => void }) {
  const { data, loading, error } = useAnalyticsQuery('quote_header', {
    quoteId: sql.string(quoteId),
  });

  useEffect(() => {
    if (data && data.length > 0) {
      onLoaded({ summaryReport: data[0].summary_report ?? null, decisionComments: data[0].decision_comments ?? null });
    }
    // onLoaded is a setState function from the parent -- stable identity, safe to omit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  return (
    <Card className="shadow-lg">
      <CardHeader>
        <CardTitle className="text-base">What needs deciding</CardTitle>
        {/* Genie is no longer on this path at all -- the detectors compute every figure and the
            Supervisor writes the prose around them. Naming a component that was removed tells a
            PM the wrong thing about where the numbers came from. */}
        <CardDescription className="text-xs">
          Hover any figure to see the measurement behind it. A wavy amber underline means there is
          no measurement behind it &mdash; check that one before acting on it.
        </CardDescription>
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
          <div className="text-xs text-muted-foreground">
            Raised by {data[0].created_by} · {data[0].created_at ? new Date(data[0].created_at).toLocaleString() : '—'}
            {data[0].teams_sent_at && <> · Teams card sent {new Date(data[0].teams_sent_at).toLocaleString()}</>}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function QuoteLinesCard({
  quoteId,
  onDecided,
  summaryReport,
}: {
  quoteId: string;
  onDecided: (result: DecisionResult) => void;
  summaryReport: string | null;
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
        reason: d[lineKey]?.reason ?? null,
        decision: d[lineKey]?.decision === decision ? null : decision,
      },
    }));
  }

  function setDraftNote(lineKey: number, note: string) {
    setDrafts((d) => ({
      ...d,
      [lineKey]: { decision: d[lineKey]?.decision ?? null, note, reason: d[lineKey]?.reason ?? null },
    }));
  }

  function setDraftReason(lineKey: number, reason: ReasonCode | null) {
    setDrafts((d) => ({
      ...d,
      [lineKey]: { decision: d[lineKey]?.decision ?? null, note: d[lineKey]?.note ?? '', reason },
    }));
  }

  const stagedLines = Object.entries(drafts)
    .filter(([, d]) => d.decision !== null)
    .map(([lineKey, d]) => ({
      lineKey: Number(lineKey),
      decision: d.decision as 'APPROVED' | 'REJECTED',
      note: d.note,
      reason: d.reason ?? undefined,
    }));

  // A rejection without a reason is the one thing the server refuses, so block it here rather
  // than letting the PM press submit and get an error back.
  const rejectionsMissingReason = stagedLines.filter((l) => l.decision === 'REJECTED' && !l.reason);

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
    <div className="space-y-4">
      <DecisionBoard
        summaryReport={summaryReport}
        lines={data ?? null}
        loading={loading}
        error={error}
        drafts={drafts}
        submitting={submitState.status === 'submitting'}
        setDraftDecision={setDraftDecision}
        setDraftNote={setDraftNote}
        setDraftReason={setDraftReason}
      />

      {submitState.status === 'error' && (
        <Alert variant="destructive">
          <AlertDescription className="text-xs">{submitState.message}</AlertDescription>
        </Alert>
      )}

      {/* Sticky, because the decisions are made down the page and the submit used to sit below
          all of them -- on a four-item quote that is a long scroll back to a button. */}
      {data && data.length > 0 && (
        <div className="sticky bottom-0 -mx-1 flex items-center justify-between gap-3 rounded-lg border bg-card/95 px-4 py-3 shadow-lg backdrop-blur">
          <span className="text-sm text-muted-foreground">
            {rejectionsMissingReason.length > 0
              ? `${rejectionsMissingReason.length} rejection${rejectionsMissingReason.length === 1 ? ' needs' : 's need'} a reason`
              : `${stagedLines.length} of ${data.length} decided`}
          </span>
          <Button
            disabled={
              stagedLines.length === 0 ||
              rejectionsMissingReason.length > 0 ||
              submitState.status === 'submitting'
            }
            onClick={submitAll}
          >
            {submitState.status === 'submitting' ? 'Submitting…' : 'Submit decisions'}
          </Button>
        </div>
      )}
    </div>
  );
}
