import { Badge } from '@databricks/appkit-ui/react';
import { cn } from '../lib/utils';

// Renders the Supervisor's summary_report -- a deterministic labelled-line
// text format (see SUPERVISOR_INSTRUCTIONS' OUTPUT CONTRACT in
// scripts/create_supervisor_agent.py). That format exists because the SAME
// text also has to survive a Microsoft Teams card's 600-character
// truncation and render correctly in that plain-text surface -- so the
// Supervisor cannot be asked to emit markdown or HTML for this page's sake.
// Instead this component parses the fixed label structure back out and
// renders it as a real report. If parsing fails (an older quote, or a
// report that doesn't match the contract), it falls back to the raw text
// rather than showing nothing.

type ParsedOption = { tag: 'CHOSEN' | 'ALT'; option: string; cost: string; leadTime: string; note: string };
type ParsedEvidence = { source: string; finding: string };

type ParsedReport = {
  recommendation: string | null;
  decisionValue: string | null;
  exposure: string | null;
  decisionValueRaw: string | null;
  signalType: string | null;
  partId: string | null;
  warehouseId: string | null;
  stockLine: string | null;
  onHand: number | null;
  safetyStock: number | null;
  whyNow: string | null;
  ifApprovedWrong: string | null;
  ifRejectedRight: string | null;
  options: ParsedOption[];
  evidence: ParsedEvidence[];
  assumptions: string | null;
};

function extractField(text: string, label: string): string | null {
  const re = new RegExp(`^${label}:\\s*(.+)$`, 'm');
  const m = text.match(re);
  return m ? m[1].trim() : null;
}

// Pulls the indented lines that follow a bare header line (e.g.
// "OPTIONS CONSIDERED" / "EVIDENCE"), stopping at the next blank line or
// unindented line.
function extractBlock(text: string, header: string): string[] {
  const lines = text.split('\n');
  const idx = lines.findIndex((l) => l.trim() === header);
  if (idx === -1) return [];
  const block: string[] = [];
  for (let i = idx + 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === '') break;
    if (!/^\s/.test(line)) break;
    block.push(line.trim());
  }
  return block;
}

function parseSummaryReport(text: string): ParsedReport {
  const recommendation = extractField(text, 'RECOMMENDATION');
  const decisionValueRaw = extractField(text, 'DECISION VALUE');
  const signalRaw = extractField(text, 'SIGNAL');
  const whyNow = extractField(text, 'WHY NOW');
  const ifApprovedWrong = extractField(text, 'IF APPROVED AND WRONG');
  const ifRejectedRight = extractField(text, 'IF REJECTED AND RIGHT');
  const assumptions = extractField(text, 'ASSUMPTIONS');

  let decisionValue: string | null = null;
  let exposure: string | null = null;
  if (decisionValueRaw) {
    // Current shape: "Rs <dv> (Rs <exposure> at risk, ranked after allowing ...)".
    // action_cost is deliberately not printed and not parsed -- it is a
    // ranking heuristic, not a quotable cost, and the bar below only needs
    // the proportion decisionValue/exposure.
    const current = decisionValueRaw.match(/Rs\s*([\d,]+).*?Rs\s*([\d,]+)\s*at risk/i);
    // Legacy shape, still on every quote written before that change.
    const legacy = decisionValueRaw.match(/Rs\s*([\d,]+).*?exposure Rs\s*([\d,]+).*?less Rs\s*([\d,]+)\s*to act/i);
    if (current) {
      [, decisionValue, exposure] = current;
    } else if (legacy) {
      [, decisionValue, exposure] = legacy;
    } else {
      const bare = decisionValueRaw.match(/Rs\s*([\d,]+)/);
      if (bare) decisionValue = bare[1];
    }
  }

  let signalType: string | null = null;
  let partId: string | null = null;
  let warehouseId: string | null = null;
  let stockLine: string | null = null;
  let onHand: number | null = null;
  let safetyStock: number | null = null;
  if (signalRaw) {
    const parts = signalRaw.split('|').map((p) => p.trim());
    if (parts.length >= 3) {
      signalType = parts[0];
      const [pId, wId] = parts[1].split('@').map((p) => p.trim());
      partId = pId ?? null;
      warehouseId = wId ?? null;
      stockLine = parts[2];
      const stockMatch = parts[2].match(/([\d,]+)\s*on hand vs\s*([\d,]+)\s*safety stock/i);
      if (stockMatch) {
        onHand = Number(stockMatch[1].replace(/,/g, ''));
        safetyStock = Number(stockMatch[2].replace(/,/g, ''));
      }
    }
  }

  const options: ParsedOption[] = extractBlock(text, 'OPTIONS CONSIDERED').flatMap((line) => {
    const m = line.match(/^\[(CHOSEN|ALT)\]\s*(.+)$/);
    if (!m) return [];
    const segments = m[2].split('|').map((s) => s.trim());
    return [
      {
        tag: m[1] as 'CHOSEN' | 'ALT',
        option: segments[0] ?? '',
        cost: segments[1] ?? '',
        leadTime: segments[2] ?? '',
        note: segments[3] ?? '',
      },
    ];
  });

  const evidence: ParsedEvidence[] = extractBlock(text, 'EVIDENCE').flatMap((line) => {
    const idx = line.indexOf(':');
    if (idx === -1) return [];
    return [{ source: line.slice(0, idx).trim(), finding: line.slice(idx + 1).trim() }];
  });

  return {
    recommendation,
    decisionValue,
    exposure,
    decisionValueRaw,
    signalType,
    partId,
    warehouseId,
    stockLine,
    onHand,
    safetyStock,
    whyNow,
    ifApprovedWrong,
    ifRejectedRight,
    options,
    evidence,
    assumptions,
  };
}

type Verdict = {
  label: string;
  toneClass: string; // banner background/border/text
  badgeClass: string;
};

function classifyRecommendation(recommendation: string | null): Verdict {
  const text = (recommendation ?? '').toUpperCase();
  if (text.startsWith('VERIFY DATA')) {
    return {
      label: 'DATA ANOMALY',
      toneClass: 'bg-destructive/10 border-destructive/30 text-destructive',
      badgeClass: 'bg-destructive text-destructive-foreground',
    };
  }
  if (text.startsWith('ESCALATE')) {
    return {
      label: 'ESCALATE',
      toneClass: 'bg-destructive/10 border-destructive/30 text-destructive',
      badgeClass: 'bg-destructive text-destructive-foreground',
    };
  }
  if (text.startsWith('EXPEDITE')) {
    return {
      label: 'STALLED — EXPEDITE',
      toneClass: 'bg-amber-500/10 border-amber-500/30 text-amber-700 dark:text-amber-400',
      badgeClass: 'bg-amber-500 text-white',
    };
  }
  return {
    label: 'RECOMMENDED ACTION',
    toneClass: 'bg-primary/5 border-primary/30 text-foreground',
    badgeClass: 'bg-primary text-primary-foreground',
  };
}

function StockBar({ onHand, safetyStock }: { onHand: number; safetyStock: number }) {
  const ratio = safetyStock > 0 ? onHand / safetyStock : 1;
  const pct = Math.max(0, Math.min(ratio, 1.5)) / 1.5 * 100;
  const isShort = onHand < safetyStock;
  return (
    <div className="flex items-center gap-2 w-full max-w-[220px]">
      <div className="relative h-2 flex-1 rounded-full bg-muted overflow-hidden">
        <div
          className={cn('absolute inset-y-0 left-0 rounded-full', isShort ? 'bg-destructive' : 'bg-emerald-500')}
          style={{ width: `${pct}%` }}
        />
        <div className="absolute inset-y-0 border-r-2 border-foreground/40" style={{ left: `${(1 / 1.5) * 100}%` }} />
      </div>
      <span className={cn('text-xs font-medium whitespace-nowrap', isShort ? 'text-destructive' : 'text-emerald-600')}>
        {onHand.toLocaleString('en-IN')} / {safetyStock.toLocaleString('en-IN')}
      </span>
    </div>
  );
}

function DecisionValueBar({ exposure, decisionValue }: { exposure: string; decisionValue: string }) {
  const exposureN = Number(exposure.replace(/,/g, ''));
  const decisionValueN = Number(decisionValue.replace(/,/g, ''));
  if (!exposureN || Number.isNaN(exposureN)) return null;
  const decisionPct = Math.max(0, Math.min((decisionValueN / exposureN) * 100, 100));
  return (
    <div className="space-y-1.5">
      <div className="h-3 w-full rounded-full bg-muted overflow-hidden flex">
        <div className="bg-emerald-500 h-full" style={{ width: `${decisionPct}%` }} title={`Rs ${decisionValue} realized`} />
        <div className="bg-muted-foreground/30 h-full flex-1" title="Allowance for the cost of the cheapest viable fix" />
      </div>
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>
          <span className="text-emerald-600 font-medium">Rs {decisionValue}</span> net value if acted on
        </span>
        <span>Rs {exposure} total exposure</span>
      </div>
    </div>
  );
}

// A run can now surface the top action for more than one signal type in a
// single quote (see invoke_supervisor.py's turn-per-candidate loop), each
// analysis artifact starting with a `## CANDIDATE i of N -- <signal_type>`
// marker line. Split on that marker before parsing; a report with no marker
// (every quote_metadata row before this change) comes back as a single
// block, so old quotes render exactly as before.
const CANDIDATE_MARKER_RE = /^##\s*CANDIDATE\s+(\d+)\s+of\s+(\d+).*$/gm;

function splitIntoBlocks(text: string): string[] {
  const markers = [...text.matchAll(CANDIDATE_MARKER_RE)];
  if (markers.length === 0) return [text];

  const blocks: string[] = [];
  for (let i = 0; i < markers.length; i++) {
    const start = markers[i].index ?? 0;
    const end = i + 1 < markers.length ? (markers[i + 1].index ?? text.length) : text.length;
    blocks.push(text.slice(start, end).trim());
  }
  return blocks;
}

function IntelligenceReportBlock({ text }: { text: string }) {
  const parsed = parseSummaryReport(text);

  // Nothing recognisable -- fall back rather than show a half-empty report.
  if (!parsed.recommendation) {
    return <pre className="whitespace-pre-wrap text-sm font-mono bg-muted/50 rounded-md p-4">{text}</pre>;
  }

  const verdict = classifyRecommendation(parsed.recommendation);

  return (
    <div className="space-y-4">
      <div className={cn('rounded-lg border p-4 space-y-2', verdict.toneClass)}>
        <div className="flex items-center gap-2">
          <span className={cn('text-[10px] font-bold tracking-wide uppercase px-2 py-0.5 rounded', verdict.badgeClass)}>
            {verdict.label}
          </span>
          {parsed.signalType && (
            <Badge variant="outline" className="font-mono text-[10px]">
              {parsed.signalType}
            </Badge>
          )}
        </div>
        <p className="text-base font-semibold leading-snug">{parsed.recommendation}</p>
      </div>

      {parsed.decisionValue && parsed.exposure ? (
        <DecisionValueBar exposure={parsed.exposure} decisionValue={parsed.decisionValue} />
      ) : parsed.decisionValueRaw ? (
        <p className="text-sm">{parsed.decisionValueRaw}</p>
      ) : null}

      {(parsed.partId || parsed.stockLine) && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {parsed.partId && (
            <span className="font-mono font-medium">
              {parsed.partId} @ {parsed.warehouseId}
            </span>
          )}
          {parsed.onHand !== null && parsed.safetyStock !== null ? (
            <StockBar onHand={parsed.onHand} safetyStock={parsed.safetyStock} />
          ) : (
            parsed.stockLine && <span className="text-muted-foreground">{parsed.stockLine}</span>
          )}
        </div>
      )}

      {parsed.whyNow && (
        <p className="text-sm text-muted-foreground leading-relaxed border-l-2 border-muted pl-3">{parsed.whyNow}</p>
      )}

      {(parsed.ifApprovedWrong || parsed.ifRejectedRight) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {parsed.ifApprovedWrong && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
              <div className="text-[10px] font-bold uppercase tracking-wide text-amber-700 dark:text-amber-400 mb-1">
                If approved &amp; wrong
              </div>
              <div className="text-sm">{parsed.ifApprovedWrong}</div>
            </div>
          )}
          {parsed.ifRejectedRight && (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
              <div className="text-[10px] font-bold uppercase tracking-wide text-destructive mb-1">
                If rejected &amp; right
              </div>
              <div className="text-sm">{parsed.ifRejectedRight}</div>
            </div>
          )}
        </div>
      )}

      {parsed.options.length > 0 && (
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground mb-1.5">Options considered</div>
          <div className="space-y-1.5">
            {parsed.options.map((opt, i) => (
              <div
                key={i}
                className={cn(
                  'rounded-md border p-2.5 text-sm flex flex-wrap items-baseline gap-x-3 gap-y-0.5',
                  opt.tag === 'CHOSEN' ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-muted bg-muted/30 text-muted-foreground',
                )}
              >
                <Badge variant={opt.tag === 'CHOSEN' ? 'default' : 'outline'} className="text-[10px]">
                  {opt.tag}
                </Badge>
                <span className="font-medium text-foreground">{opt.option}</span>
                {opt.cost && <span>{opt.cost}</span>}
                {opt.leadTime && <span>{opt.leadTime}</span>}
                {opt.note && <span className="italic">{opt.note}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {parsed.evidence.length > 0 && (
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground mb-1.5">Evidence</div>
          <ul className="space-y-1">
            {parsed.evidence.map((e, i) => (
              <li key={i} className="text-sm flex gap-2">
                <code className="text-[10px] bg-muted px-1.5 py-0.5 rounded font-mono whitespace-nowrap h-fit mt-0.5">
                  {e.source}
                </code>
                <span className="text-muted-foreground">{e.finding}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {parsed.assumptions && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2.5 text-sm">
          <span className="font-semibold text-amber-700 dark:text-amber-400">Assumption: </span>
          {parsed.assumptions}
        </div>
      )}
    </div>
  );
}

export function IntelligenceReport({ text }: { text: string }) {
  const blocks = splitIntoBlocks(text);

  // Single block (no candidate marker, or exactly one candidate) -- render
  // exactly as before, no extra wrapper chrome.
  if (blocks.length <= 1) {
    return <IntelligenceReportBlock text={blocks[0] ?? text} />;
  }

  return (
    <div className="space-y-6">
      {blocks.map((block, i) => (
        <div key={i} className={i > 0 ? 'pt-6 border-t' : undefined}>
          <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground mb-2">
            Candidate {i + 1} of {blocks.length}
          </div>
          <IntelligenceReportBlock text={block} />
        </div>
      ))}
    </div>
  );
}
