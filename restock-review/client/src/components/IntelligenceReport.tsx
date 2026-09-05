import { Badge } from '@databricks/appkit-ui/react';
import { cn } from '../lib/utils';
import {
  citableFields, citeProse, classifyRecommendation, labelFor, parseAssumptions,
  parseSummaryReport, splitIntoBlocks,
} from '../lib/quoteReport';
import type { CitableField } from '../lib/quoteReport';

// Renders the Supervisor's summary_report as an action item a Production Manager can decide on:
// the ask, the money and the clock first; the receipts one click away. The parsing and the
// citation matching live in ../lib/quoteReport so they can be tested on real stored reports.

function CitedProse({ prose, fields }: { prose: string; fields: CitableField[] }) {
  return (
    <>
      {citeProse(prose, fields).map((seg, i) =>
        seg.ref !== null ? (
          <span key={i} title={seg.title}>
            {seg.text}
            <sup className="ml-px text-[10px] font-medium text-primary">{seg.ref}</sup>
          </span>
        ) : seg.uncited ? (
          <span
            key={i}
            title={seg.title}
            className="rounded bg-amber-500/20 px-1 text-amber-800 dark:text-amber-300"
          >
            {seg.text}
            <sup className="ml-px text-[10px]">?</sup>
          </span>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  );
}


function Disclosure({ summary, count, children }: { summary: string; count?: number; children: React.ReactNode }) {
  return (
    <details className="group border-t border-border pt-2">
      <summary className="cursor-pointer list-none text-xs font-medium text-muted-foreground hover:text-foreground">
        <span className="inline-block transition-transform group-open:rotate-90">▸</span>{' '}
        {summary}
        {count !== undefined && <span className="ml-1 text-muted-foreground/70">({count})</span>}
      </summary>
      <div className="pt-2">{children}</div>
    </details>
  );
}

// --- the item card ---------------------------------------------------------

function ActionItemCard({ text, index, total }: { text: string; index?: number; total?: number }) {
  const parsed = parseSummaryReport(text);
  const assumptions = parseAssumptions(parsed.assumptions);
  const fields = citableFields(parsed.evidence);
  const verdict = classifyRecommendation(parsed.recommendation);

  if (!parsed.recommendation) {
    return <pre className="whitespace-pre-wrap font-mono text-xs text-muted-foreground">{text}</pre>;
  }

  const money = parsed.exposure
    ? `Rs ${parsed.exposure}`
    : parsed.decisionValueRaw?.replace(/\s*\(.*$/, '') ?? null;
  const cover = fields.find((f) => f.field.endsWith('days_of_cover'));

  return (
    <div className={cn('rounded-lg border bg-card', verdict.toneClass)}>
      {/* The ask, the money and the clock -- everything needed to decide, before any scrolling. */}
      <div className="p-4 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {index && total ? `Action item ${index} of ${total}` : 'Action item'}
          </span>
          <div className="flex items-center gap-1.5">
            <span className={cn('rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide', verdict.badgeClass)}>
              {verdict.label}
            </span>
            {parsed.signalType && (
              <Badge variant="outline" className="font-mono text-[10px]">{parsed.signalType}</Badge>
            )}
          </div>
        </div>

        <p className="text-base font-semibold leading-snug">{parsed.recommendation}</p>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          {money && <span className="font-medium">{money} at risk</span>}
          {cover && (
            <>
              <span className="text-muted-foreground">·</span>
              <span className="text-muted-foreground">{cover.display} days of cover left</span>
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

      {parsed.whyNow && (
        <div className="border-t border-border px-4 py-3">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Why now</div>
          <p className="text-sm leading-relaxed">
            <CitedProse prose={parsed.whyNow} fields={fields} />
          </p>
        </div>
      )}

      {parsed.previouslyDecided.length > 0 && (
        <div className="border-t border-border bg-amber-500/5 px-4 py-3">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-400">
            You decided this before
          </div>
          <ul className="space-y-0.5 text-sm">
            {parsed.previouslyDecided.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}

      {(parsed.ifApprovedWrong || parsed.ifRejectedRight) && (
        <div className="grid grid-cols-1 gap-px border-t border-border bg-border sm:grid-cols-2">
          {parsed.ifApprovedWrong && (
            <div className="bg-card px-4 py-3">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                If you approve and it wasn't needed
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
                    o.tag === 'CHOSEN' ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400' : 'bg-muted text-muted-foreground')}>
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
                          ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400'
                          : 'bg-amber-500/20 text-amber-700 dark:text-amber-400')}>
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
