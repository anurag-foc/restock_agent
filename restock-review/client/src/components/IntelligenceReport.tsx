import {
  Badge, Collapsible, CollapsibleContent, CollapsibleTrigger,
  HoverCard, HoverCardContent, HoverCardTrigger,
} from '@databricks/appkit-ui/react';
import { ChevronRight } from 'lucide-react';
import { cn } from '../lib/utils';
import {
  citableFields, citeProse, classifyRecommendation, labelFor, parseAssumptions,
  parseSummaryReport, splitIntoBlocks,
} from '../lib/quoteReport';
import type { CitableField } from '../lib/quoteReport';

// Renders the Supervisor's summary_report as an action item a Production Manager can decide on:
// the ask, the money and the clock first; the receipts one click away. The parsing and the
// citation matching live in ../lib/quoteReport so they can be tested on real stored reports.

// Whole class names, because Tailwind scans source text -- an interpolated `border-l-${token}`
// would be dropped at build time and every card would come out borderless.
const ACCENT_BORDER: Record<string, string> = {
  'chart-cat-1': 'border-l-chart-cat-1',
  'chart-cat-2': 'border-l-chart-cat-2',
  'chart-cat-3': 'border-l-chart-cat-3',
  'chart-cat-4': 'border-l-chart-cat-4',
  'chart-cat-5': 'border-l-chart-cat-5',
  'chart-cat-6': 'border-l-chart-cat-6',
  'chart-cat-8': 'border-l-chart-cat-8',
  'chart-cat-7': 'border-l-chart-cat-7',
  destructive: 'border-l-destructive',
};

const ACCENT_CHIP: Record<string, string> = {
  'chart-cat-1': 'bg-chart-cat-1/12 text-chart-cat-1',
  'chart-cat-2': 'bg-chart-cat-2/12 text-chart-cat-2',
  'chart-cat-3': 'bg-chart-cat-3/12 text-chart-cat-3',
  'chart-cat-4': 'bg-chart-cat-4/12 text-chart-cat-4',
  'chart-cat-5': 'bg-chart-cat-5/12 text-chart-cat-5',
  'chart-cat-6': 'bg-chart-cat-6/12 text-chart-cat-6',
  'chart-cat-8': 'bg-chart-cat-8/12 text-chart-cat-8',
  'chart-cat-7': 'bg-chart-cat-7/12 text-chart-cat-7',
  destructive: 'bg-destructive/12 text-destructive',
};

function Citation({ text, title }: { text: string; title: string }) {
  const split = title.indexOf(': ');
  const [label, value] = split === -1 ? [title, ''] : [title.slice(0, split), title.slice(split + 2)];
  return (
    <HoverCard openDelay={120}>
      <HoverCardTrigger asChild>
        <span className="cursor-help underline decoration-dotted decoration-muted-foreground/40 underline-offset-2">
          {text}
        </span>
      </HoverCardTrigger>
      <HoverCardContent className="w-auto max-w-xs py-2">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="font-mono text-sm">{value}</div>
      </HoverCardContent>
    </HoverCard>
  );
}

function CitedProse({ prose, fields }: { prose: string; fields: CitableField[] }) {
  return (
    <>
      {citeProse(prose, fields).map((seg, i) =>
        seg.ref !== null ? (
          // Muted and small on purpose: a mark on every figure is a lot of marks in one sentence,
          // and in the accent colour they competed with the prose. The reference is there to be
          // followed when doubted, not read on the way past. A native title= tooltip is slow,
          // unstyled and invisible on touch, so the one thing a sceptical reader reaches for gets
          // a real inspectable card instead.
          <span key={i}>
            <Citation text={seg.text} title={seg.title ?? ''} />
            <sup className="ml-px text-[9px] font-normal text-muted-foreground/70">{seg.ref}</sup>
          </span>
        ) : seg.uncited ? (
          <HoverCard key={i} openDelay={120}>
            <HoverCardTrigger asChild>
              <span className="cursor-help text-warning underline decoration-wavy decoration-warning underline-offset-2">
                {seg.text}
                <sup className="ml-px text-[9px] font-semibold">?</sup>
              </span>
            </HoverCardTrigger>
            <HoverCardContent className="w-auto max-w-xs py-2 text-xs">{seg.title}</HoverCardContent>
          </HoverCard>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  );
}

function Disclosure({ summary, count, children }: { summary: string; count?: number; children: React.ReactNode }) {
  return (
    <Collapsible className="group border-t border-border pt-2">
      <CollapsibleTrigger className="flex w-full items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">
        <ChevronRight className="size-3 transition-transform group-data-[state=open]:rotate-90" />
        {summary}
        {count !== undefined && <span className="text-muted-foreground/70">({count})</span>}
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-2">{children}</CollapsibleContent>
    </Collapsible>
  );
}

// --- the item card ---------------------------------------------------------

export function ActionItemCard({
  text,
  index,
  total,
  footer,
  actionType,
}: {
  text: string;
  index?: number;
  total?: number;
  /** From the line itself (TRANSFER / PURCHASE / RECALIBRATE / ...), which is more reliable than
   *  reading a verb out of the recommendation prose. */
  actionType?: string | null;
  // The decide controls, rendered inside this card. Reasoning and decision belong in one place:
  // when they were a report card and a separate table row, deciding meant reading item 2 here and
  // finding row 2 down there, with nothing tying them together.
  footer?: React.ReactNode;
}) {
  const parsed = parseSummaryReport(text);
  const assumptions = parseAssumptions(parsed.assumptions);
  const exposureValue = parsed.exposure ? Number(parsed.exposure.replace(/,/g, '')) : null;
  const fields = citableFields(
    parsed.evidence,
    exposureValue !== null && !Number.isNaN(exposureValue)
      ? [{ label: 'Money at risk', display: `Rs ${parsed.exposure}`, value: exposureValue }]
      : [],
  );
  const verdict = classifyRecommendation(parsed.recommendation, actionType);

  if (!parsed.recommendation) {
    return <pre className="whitespace-pre-wrap font-mono text-xs text-muted-foreground">{text}</pre>;
  }

  const money = parsed.exposure
    ? `Rs ${parsed.exposure}`
    : parsed.decisionValueRaw?.replace(/\s*\(.*$/, '') ?? null;
  const coverField = fields.find((f) => f.field.endsWith('days_of_cover'));
  // Only when it is actually a number. Quotes written before narration stopped emitting Python
  // None still carry it, and "None days of cover left" reads as a broken page rather than as the
  // real state it describes (stock that is not moving at all has no days of cover).
  const cover = coverField && coverField.value !== null ? coverField : null;

  return (
    <div
      className={cn(
        'overflow-hidden rounded-lg border border-l-4 bg-card',
        ACCENT_BORDER[verdict.accent] ?? 'border-l-border',
        verdict.tone === 'destructive' && 'border-destructive/40',
        verdict.tone === 'warning' && 'border-warning/40',
      )}
    >
      {/* The ask, the money and the clock -- everything needed to decide, before any scrolling. */}
      <div className="p-4 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {index && total ? `Action item ${index} of ${total}` : 'Action item'}
          </span>
          <div className="flex items-center gap-1.5">
            <span
              className={cn(
                'rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                ACCENT_CHIP[verdict.accent] ?? 'bg-muted text-muted-foreground',
                verdict.tone === 'warning' && 'bg-warning/15 text-warning',
              )}
            >
              {verdict.label}
            </span>
            {parsed.signalType && (
              <Badge variant="outline" className="font-mono text-[10px]">{parsed.signalType}</Badge>
            )}
          </div>
        </div>

        <p className="text-base font-semibold leading-snug">
          {parsed.recommendation.charAt(0).toUpperCase() + parsed.recommendation.slice(1)}
        </p>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          {money && (
            <span className="text-base font-semibold tabular-nums text-foreground">
              {money} <span className="text-xs font-normal text-muted-foreground">at risk</span>
            </span>
          )}
          {cover && (
            <>
              <span className="text-muted-foreground">·</span>
              <span
                className={cn(
                  'font-medium tabular-nums',
                  (cover.value ?? 999) < 7
                    ? 'text-destructive'
                    : (cover.value ?? 999) < 21
                      ? 'text-warning'
                      : 'text-muted-foreground',
                )}
              >
                {cover.display} days of cover left
              </span>
            </>
          )}
          {parsed.partId && (
            <>
              <span className="text-muted-foreground">·</span>
              <span className="font-mono text-xs text-muted-foreground">
                {parsed.partId}{parsed.warehouseId ? ` @ ${parsed.warehouseId}` : ''}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Straight under the figure, because "where does that number come from" is the first
          question anyone asks about it and the answer differs by finding type -- a stockout is
          probability times consequence, dead capital is a carrying cost, a cascade is blocked
          production. Built by the scanner that had the inputs, not reconstructed here. */}
      {parsed.exposureBasis && (
        <div className="px-4 pb-3 -mt-1 text-xs leading-relaxed text-muted-foreground">
          {parsed.exposureBasis}
        </div>
      )}

      {parsed.whyNow && (
        <div className="border-t border-border px-4 py-3">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Why now</div>
          <p className="text-sm leading-relaxed">
            <CitedProse prose={parsed.whyNow} fields={fields} />
          </p>
        </div>
      )}

      {parsed.previouslyDecided.length > 0 && (
        <div className="border-t border-border bg-warning/5 px-4 py-3">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-warning">
            You decided this before
          </div>
          <ul className="space-y-0.5 text-sm">
            {parsed.previouslyDecided.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}

      {/* Paired only when both exist. The brief usually emits just the approve-side outcome, and
          a fixed two-column grid left a grey empty cell next to it on every card. */}
      {(parsed.ifApprovedWrong || parsed.ifRejectedRight) && (
        <div
          className={cn(
            'grid grid-cols-1 gap-px border-t border-border bg-border',
            parsed.ifApprovedWrong && parsed.ifRejectedRight && 'sm:grid-cols-2',
          )}
        >
          {parsed.ifApprovedWrong && (
            <div className="bg-card px-4 py-3">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                If you approve and it wasn&apos;t needed
              </div>
              <p className="text-sm"><CitedProse prose={parsed.ifApprovedWrong} fields={fields} /></p>
            </div>
          )}
          {parsed.ifRejectedRight && (
            <div className="bg-card px-4 py-3">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                If you reject and it was needed
              </div>
              <p className="text-sm"><CitedProse prose={parsed.ifRejectedRight} fields={fields} /></p>
            </div>
          )}
        </div>
      )}

      <div className="space-y-1 px-4 pb-3 pt-2">
        {parsed.options.length > 0 && (
          <Disclosure summary="Other options considered" count={parsed.options.length}>
            <ul className="space-y-1.5 text-sm">
              {parsed.options.map((o, i) => (
                <li key={i} className="flex flex-wrap items-baseline gap-x-2">
                  <span className={cn('rounded px-1.5 py-px text-[10px] font-bold uppercase',
                    o.tag === 'CHOSEN' ? 'bg-success/15 text-success' : 'bg-muted text-muted-foreground')}>
                    {o.tag === 'CHOSEN' ? 'chosen' : 'not chosen'}
                  </span>
                  <span>{o.option}</span>
                  {o.cost && <span className="text-muted-foreground">— {o.cost}</span>}
                  {o.leadTime && <span className="text-muted-foreground">· {o.leadTime}</span>}
                  {o.note && <span className="text-muted-foreground">· {o.note}</span>}
                </li>
              ))}
            </ul>
          </Disclosure>
        )}

        {fields.length > 0 && (
          <Disclosure summary="Where these numbers come from" count={fields.length}>
            <table className="w-full text-sm">
              <tbody>
                {fields.map((f) => (
                  <tr key={f.ref} className="border-b border-border/50 last:border-0">
                    <td className="w-8 py-1 align-top text-[10px] font-medium text-primary">{f.ref}</td>
                    <td className="py-1 pr-3 align-top text-muted-foreground">{f.label}</td>
                    <td className="py-1 text-right align-top font-mono text-xs">{f.display}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Disclosure>
        )}

        {parsed.assumptions && (
          <Disclosure summary="Policy settings used" count={assumptions.length || undefined}>
            {assumptions.length === 0 ? (
              <p className="text-sm text-muted-foreground">{parsed.assumptions}</p>
            ) : (
              <ul className="space-y-1 text-sm">
                {assumptions.map((a, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-x-1.5">
                    <span className="font-medium">{labelFor(a.name)}</span>
                    <span className="font-mono text-xs">{a.value}</span>
                    {a.kind && (
                      <span className={cn('rounded px-1 py-px text-[10px] uppercase tracking-wide',
                        a.kind.toLowerCase() === 'measured'
                          ? 'bg-success/15 text-success'
                          : 'bg-warning/20 text-warning')}>
                        {a.kind}
                      </span>
                    )}
                    {a.basis && <span className="text-muted-foreground">{a.basis}</span>}
                  </li>
                ))}
              </ul>
            )}
          </Disclosure>
        )}
      </div>

      {footer && <div className="border-t border-border">{footer}</div>}
    </div>
  );
}

export function IntelligenceReport({ text }: { text: string }) {
  const blocks = splitIntoBlocks(text);
  if (blocks.length <= 1) return <ActionItemCard text={blocks[0] ?? text} />;
  return (
    <div className="space-y-4">
      {blocks.map((block, i) => (
        <ActionItemCard key={i} text={block} index={i + 1} total={blocks.length} />
      ))}
    </div>
  );
}
