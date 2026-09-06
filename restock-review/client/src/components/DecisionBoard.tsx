import {
  Alert, AlertDescription, Badge, Button, Empty, EmptyDescription, EmptyHeader, EmptyTitle,
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Skeleton, Textarea,
} from '@databricks/appkit-ui/react';
import { Check, X } from 'lucide-react';
import { cn } from '../lib/utils';
import { ActionItemCard } from './IntelligenceReport';
import { parseSummaryReport, splitIntoBlocks } from '../lib/quoteReport';

// The reasoning and the decision belong in one place. They used to be a report card at the top of
// the page and an eight-column table underneath, so deciding meant reading "action item 2" up here
// and then finding row 2 down there with nothing tying the two together -- and a table is the
// wrong container for four decisions that each need a paragraph of context.
//
// Pairing is by position: persist_quote writes REQ-<quote>-1..N in the order the brief lists the
// items, and quote_lines orders by RESTOCK_REQUEST_KEY. That is reliable, but pairing a decision
// with the WRONG item would let a PM approve something they did not read, so it is verified
// rather than assumed -- see `pairItemsWithLines`.

export type ReasonCode =
  | 'NOT_A_PROBLEM' | 'ALREADY_HANDLED' | 'CANNOT_ACT_NOW' | 'NUMBERS_WRONG' | 'OTHER';

export const REASON_OPTIONS: { value: ReasonCode; label: string; hint: string }[] = [
  { value: 'CANNOT_ACT_NOW', label: "Can't act right now", hint: 'Comes back in two weeks' },
  { value: 'ALREADY_HANDLED', label: 'Already handled', hint: 'Returns if it gets worse' },
  { value: 'NOT_A_PROBLEM', label: 'Not a real problem', hint: 'Closed unless the stakes grow' },
  { value: 'NUMBERS_WRONG', label: 'Numbers look wrong', hint: 'Closed, and flagged for review' },
  { value: 'OTHER', label: 'Other', hint: 'Closed unless the stakes grow' },
];

export type Draft = { decision: 'APPROVED' | 'REJECTED' | null; note: string; reason: ReasonCode | null };

export type DecidableLine = {
  RESTOCK_REQUEST_KEY: number;
  PART_ID: string | null;
  WAREHOUSE_ID: string | null;
  REQUEST_STATUS: string;
  NOTE: string | null;
  ACTION_TYPE?: string | null;
};

/** Position pairing, only when it can be shown to be right.
 *
 * Requires one report item per line, and every line that names a part to have that part named in
 * its own item's recommendation. If either fails the caller falls back to showing the report and
 * the decisions separately -- less pleasant, but a PM approving a decision attached to someone
 * else's reasoning is the one outcome worth any amount of ugliness to avoid.
 */
export function pairItemsWithLines(blocks: string[], lines: DecidableLine[]): boolean {
  if (blocks.length === 0 || blocks.length !== lines.length) return false;
  return lines.every((line, i) => {
    if (!line.PART_ID) return true; // a supplier-grain line names no part to check against
    const recommendation = parseSummaryReport(blocks[i]).recommendation ?? '';
    return recommendation.includes(line.PART_ID);
  });
}

function DecisionBar({
  line, draft, disabled, onDecision, onNote, onReason,
}: {
  line: DecidableLine;
  draft: Draft;
  disabled: boolean;
  onDecision: (d: 'APPROVED' | 'REJECTED') => void;
  onNote: (note: string) => void;
  onReason: (r: ReasonCode | null) => void;
}) {
  const actionable = line.REQUEST_STATUS === 'PENDING_APPROVAL' || line.REQUEST_STATUS === 'NEEDS_REVIEW';

  if (!actionable) {
    return (
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 text-sm">
        <Badge variant="secondary">{line.REQUEST_STATUS.replace(/_/g, ' ').toLowerCase()}</Badge>
        {line.NOTE && <span className="text-muted-foreground">{line.NOTE}</span>}
      </div>
    );
  }

  return (
    <div className="space-y-2 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant={draft.decision === 'APPROVED' ? 'default' : 'outline'}
          disabled={disabled}
          onClick={() => onDecision('APPROVED')}
        >
          <Check className="size-3.5" /> Approve
        </Button>
        <Button
          size="sm"
          variant={draft.decision === 'REJECTED' ? 'destructive' : 'outline'}
          disabled={disabled}
          onClick={() => onDecision('REJECTED')}
        >
          <X className="size-3.5" /> Reject
        </Button>

        {draft.decision === 'REJECTED' && (
          <Select
            value={draft.reason ?? ''}
            disabled={disabled}
            onValueChange={(v) => onReason((v || null) as ReasonCode | null)}
          >
            <SelectTrigger size="sm" className={cn('w-[190px]', !draft.reason && 'border-destructive')}>
              <SelectValue placeholder="Why? (required)" />
            </SelectTrigger>
            <SelectContent>
              {REASON_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        {/* Say what the choice DOES. Picking between labels with no stated consequence is
            guessing, and this one decides whether a real problem comes back or is buried. */}
        {draft.decision === 'REJECTED' && (
          <span className="text-xs text-muted-foreground">
            {REASON_OPTIONS.find((o) => o.value === draft.reason)?.hint ??
              'This decides whether it comes back.'}
          </span>
        )}
      </div>

      {draft.decision && (
        <Textarea
          className="min-h-[38px] text-sm"
          placeholder={
            draft.decision === 'REJECTED'
              ? 'Anything worth telling yourself next time? (optional)'
              : 'Note for whoever acts on this (optional)'
          }
          value={draft.note}
          disabled={disabled}
          onChange={(e) => onNote(e.target.value)}
        />
      )}
    </div>
  );
}

export function DecisionBoard({
  summaryReport, lines, loading, error, drafts, submitting,
  setDraftDecision, setDraftNote, setDraftReason,
}: {
  summaryReport: string | null;
  lines: DecidableLine[] | null;
  loading: boolean;
  error: string | null;
  drafts: Record<number, Draft>;
  submitting: boolean;
  setDraftDecision: (k: number, d: 'APPROVED' | 'REJECTED') => void;
  setDraftNote: (k: number, note: string) => void;
  setDraftReason: (k: number, r: ReasonCode | null) => void;
}) {
  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>Could not load this quote: {error}</AlertDescription>
      </Alert>
    );
  }
  if (!lines || lines.length === 0) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>Nothing to decide</EmptyTitle>
          <EmptyDescription>This quote has no action lines.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  const blocks = splitIntoBlocks(summaryReport ?? '');
  const merged = pairItemsWithLines(blocks, lines);

  const bar = (line: DecidableLine) => (
    <DecisionBar
      line={line}
      draft={drafts[line.RESTOCK_REQUEST_KEY] ?? { decision: null, note: '', reason: null }}
      disabled={submitting}
      onDecision={(d) => setDraftDecision(line.RESTOCK_REQUEST_KEY, d)}
      onNote={(n) => setDraftNote(line.RESTOCK_REQUEST_KEY, n)}
      onReason={(r) => setDraftReason(line.RESTOCK_REQUEST_KEY, r)}
    />
  );

  if (merged) {
    return (
      <div className="space-y-4">
        {lines.map((line, i) => (
          <ActionItemCard
            key={line.RESTOCK_REQUEST_KEY}
            text={blocks[i]}
            index={i + 1}
            total={lines.length}
            actionType={line.ACTION_TYPE}
            footer={bar(line)}
          />
        ))}
      </div>
    );
  }

  // Could not prove the pairing. Show the reasoning, then the decisions, and say why they are
  // apart rather than silently guessing which paragraph belongs to which line.
  return (
    <div className="space-y-4">
      <Alert>
        <AlertDescription className="text-xs">
          The written analysis could not be matched line-for-line with the actions below, so they
          are shown separately. Read the analysis first.
        </AlertDescription>
      </Alert>
      {blocks.map((block, i) => (
        <ActionItemCard key={`b${i}`} text={block} index={i + 1} total={blocks.length} />
      ))}
      <div className="space-y-2">
        {lines.map((line) => (
          <div key={line.RESTOCK_REQUEST_KEY} className="rounded-lg border bg-card">
            <div className="px-4 pt-3 text-sm font-medium">
              {line.PART_ID ?? 'Network-wide'}{line.WAREHOUSE_ID ? ` @ ${line.WAREHOUSE_ID}` : ''}
            </div>
            {bar(line)}
          </div>
        ))}
      </div>
    </div>
  );
}
