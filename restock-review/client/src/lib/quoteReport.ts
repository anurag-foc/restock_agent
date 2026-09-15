// Pure parsing and citation logic for the Supervisor's summary_report, kept apart from the
// rendering so it can be tested without pulling the UI kit (and its chart bundle) into the test
// runner. See IntelligenceReport.tsx for what these produce on screen.
//
// **Citations are derived here, not written by the model.** Every figure in the prose is matched
// against the evidence block the model was given: a match becomes a numbered reference, and a
// figure matching nothing is flagged. Asking the model for its own citation markers would add a
// new thing for it to get wrong; matching instead turns the citation pass into a fabrication
// detector. Every fabrication on record -- `Rs 216` back-solved from action_cost, `Rs 1,700`
// holding against a real zero, a total 5.6x out -- was a figure with no evidence behind it.

export type ParsedOption = { tag: 'CHOSEN' | 'ALT'; option: string; cost: string; leadTime: string; note: string };
export type ParsedEvidence = { source: string; finding: string };

export type ParsedReport = {
  recommendation: string | null;
  decisionValue: string | null;
  exposure: string | null;
  /** What the exposure figure IS, in the brief's own words -- 'at risk' for most finding types,
   *  'of risk removed' for a transfer, whose figure is a net recovery rather than money exposed.
   *  The card used to hardcode 'at risk' under every headline figure, which mislabelled every
   *  transfer it rendered. Null on quotes written before the brief distinguished them. */
  exposureLabel: string | null;
  decisionValueRaw: string | null;
  signalType: string | null;
  partId: string | null;
  warehouseId: string | null;
  stockLine: string | null;
  onHand: number | null;
  safetyStock: number | null;
  whyNow: string | null;
  /** What it costs to leave this alone. Added in the readability pass: the old skeleton argued
   *  both for acting and about the risk of acting, and never once stated the cost of inaction
   *  plainly -- a reader had to derive it from an exposure figure several lines away. Null on
   *  every quote written before it existed. */
  ifYouDoNothing: string | null;
  ifApprovedWrong: string | null;
  ifRejectedRight: string | null;
  options: ParsedOption[];
  evidence: ParsedEvidence[];
  assumptions: string | null;
  exposureBasis: string | null;
  previouslyDecided: string[];
};

function extractField(text: string, label: string): string | null {
  const re = new RegExp(`^${label}:\\s*(.+)$`, 'm');
  const m = text.match(re);
  return m ? m[1].trim() : null;
}

// Pulls the indented lines following a bare header line, stopping at the next blank or
// unindented line.
function extractBlock(text: string, header: string): string[] {
  const lines = text.split('\n');
  const idx = lines.findIndex((l) => l.trim() === header || l.trim() === `${header}:`);
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

export function parseSummaryReport(text: string): ParsedReport {
  const recommendation = extractField(text, 'RECOMMENDATION');
  const decisionValueRaw = extractField(text, 'DECISION VALUE');
  const signalRaw = extractField(text, 'SIGNAL');
  const whyNow = extractField(text, 'WHY NOW');
  const ifYouDoNothing = extractField(text, 'IF YOU DO NOTHING');
  const ifApprovedWrong = extractField(text, 'IF APPROVED AND WRONG');
  const ifRejectedRight = extractField(text, 'IF REJECTED AND RIGHT');
  // Both labels accepted. Quotes before the intelligence-layer redesign say "ASSUMPTIONS:"; the
  // redesigned brief says "ASSUMPTIONS USED:" because the disclosure is per-finding now.
  const assumptions = extractField(text, 'ASSUMPTIONS USED') ?? extractField(text, 'ASSUMPTIONS');
  // Absent on every quote written before the derivation was carried on the finding.
  const exposureBasis = extractField(text, 'HOW THAT IS WORKED OUT');
  // The header was briefly emitted with a trailing instruction in parentheses; a handful of
  // quotes carry that shape, so both are accepted.
  const previouslyDecided = extractBlock(text, 'PREVIOUSLY DECIDED').concat(
    extractBlock(text, 'PREVIOUSLY DECIDED (quote the note verbatim; never paraphrase or soften it)'),
  );

  let decisionValue: string | null = null;
  let exposure: string | null = null;
  let exposureLabel: string | null = null;
  if (decisionValueRaw) {
    // Current shape: "Rs <dv> (Rs <exposure> at risk, ranked after allowing ...)". action_cost is
    // deliberately not printed and not parsed -- it is a ranking heuristic, not a quotable cost.
    // The scale parenthetical sits between the figure and "at risk" -- the line reads
    // "Rs 6,19,414 (6.19 lakh) (Rs 17,84,154 (17.84 lakh) at risk, ...)". Without allowing for it
    // this match failed on every costed finding, fell through to the first figure, and the card
    // showed the DECISION VALUE labelled "at risk": Rs 6.19 lakh where Rs 17.84 lakh was at
    // stake. Understating the money on a decision screen is the worst way to be wrong.
    // The noun is parsed, not assumed: a transfer's figure is `of risk removed` (FX1's net
    // benefit) and everything else's is `at risk`. Matching only the latter dropped every
    // transfer through to the bare-figure branch below, which still found the right number but
    // let the card label a recovery as an exposure.
    const current = decisionValueRaw.match(
      /Rs\s*([\d,]+)(?:\s*\([^)]*\))?\s*\(\s*Rs\s*([\d,]+)(?:\s*\([^)]*\))?\s*(at risk|of risk removed)/i,
    );
    const legacy = decisionValueRaw.match(/Rs\s*([\d,]+).*?exposure Rs\s*([\d,]+).*?less Rs\s*([\d,]+)\s*to act/i);
    if (current) {
      [, decisionValue, exposure, exposureLabel] = current;
    } else if (legacy) {
      [, decisionValue, exposure] = legacy;
    } else {
      // "Rs 6,11,97,102 (6.12 crore) at risk" -- the costless-action shape, which prints ONE
      // figure because decision value and exposure are equal when nothing is subtracted. Setting
      // only decisionValue here left exposure null, and exposure is what the card cites the
      // headline money against, so the most prominent number on every such card rendered as
      // unsupported.
      const bare = decisionValueRaw.match(/Rs\s*([\d,]+)/);
      if (bare) {
        decisionValue = bare[1];
        exposure = bare[1];
        // Dead capital's figure is an ANNUAL CARRYING COST -- narration.py has said so since the
        // first live run outranked a production block with one, but the card hardcoded 'at risk'
        // underneath it regardless, which is exactly the recurring-cost-read-as-imminent-loss
        // this branch was written to prevent.
        exposureLabel = /of risk removed/i.test(decisionValueRaw)
          ? 'of risk removed'
          : /a year to hold/i.test(decisionValueRaw)
            ? 'a year to hold'
            : 'at risk';
      }
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
    const m = line.match(/^\[(CHOSEN|NOT CHOSEN|ALT)\]\s*(.+)$/);
    if (!m) return [];
    const segments = m[2].split('|').map((s) => s.trim());
    return [
      {
        // '[NOT CHOSEN]' is the brief's wording; older quotes say '[ALT]'. Both fold to ALT.
        tag: m[1] === 'CHOSEN' ? 'CHOSEN' : 'ALT',
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
    recommendation, decisionValue, exposure, exposureLabel, decisionValueRaw, signalType, partId, warehouseId,
    stockLine, onHand, safetyStock, whyNow, ifYouDoNothing, ifApprovedWrong, ifRejectedRight, options, evidence,
    assumptions, previouslyDecided, exposureBasis,
  };
}

// --- human labels ----------------------------------------------------------
// Only the fields whose snake_case name is genuinely unclear to a planner. Anything unmapped
// falls through to a prettifier, so a new detector field renders readably on the day it ships
// rather than waiting for someone to remember this map.
const FIELD_LABELS: Record<string, string> = {
  available_qty: 'Available now',
  on_hand_qty: 'On hand',
  in_transit_qty: 'Already on order',
  safety_stock_qty: 'Safety stock',
  forward_burn: 'Used per day',
  corrected_forward_burn: 'Used per day (corrected)',
  recorded_daily_consumption: 'Used per day (as recorded)',
  days_of_cover: 'Days of cover left',
  cover_threshold_days: 'Cover it should have',
  mu_lead_days: 'Typical lead time',
  sigma_lead_days: 'Lead time swing',
  p90_lead_days: 'Worst-case lead time',
  p_stockout: 'Chance of running out',
  consequence: 'Cost if it runs out',
  consequence_basis: 'How that cost was worked out',
  burn_method: 'How usage was estimated',
  burn_confidence: 'Confidence in usage estimate',
  lead_tier: 'Lead time based on',
  unit_cost: 'Unit cost',
  effective_unit_cost: 'Unit cost incl. rejects and risk',
  trapped_value: 'Money tied up',
  annual_carrying_cost: 'Cost per year to hold',
  moq: 'Supplier minimum order',
  pack_size: 'Ships in packs of',
  orderable_qty: 'Quantity to order',
  required_qty: 'Quantity actually needed',
  excess_qty: 'Extra forced by the minimum',
  excess_holding_cost: 'Cost of holding the extra',
  excess_months: 'Months the extra sits',
  target_cover_days: 'Cover being bought',
  subtotal: 'Order value',
  action_cost: 'Cost to act',
  transfer_qty: 'Units to move',
  donor_warehouse_id: 'Moving from',
  receiver_warehouse_id: 'Moving to',
  donor_available: 'Donor has',
  receiver_available: 'Receiving site has',
  receiver_short_by: 'Receiving site short by',
  receiver_need: 'Receiving site short by',
  donor_safety_stock: 'Donor safety stock',
  donor_burn_per_day: 'Donor uses per day',
  donor_cover_after_days: 'Donor cover after the move',
  donors_considered: 'Warehouses considered',
  runner_up_donor: 'Next best donor',
  freight_cost: 'Freight',
  receiver_risk_before: 'Risk of running out now',
  receiver_risk_after: 'Risk after the move',
  donor_risk_before: 'Donor risk now',
  donor_risk_after: 'Donor risk after',
  required_units: 'Assemblies planned',
  parent_on_hand_qty: 'Assemblies in stock',
  buildable_from_children: 'Assemblies buildable from parts',
  supply_units: 'Assemblies available in total',
  units_blocked: 'Assemblies that cannot be built',
  parent_unit_cost: 'Assembly value each',
  value_at_risk: 'Production value at risk',
  binding_children: 'Blocked by',
  binding_child_count: 'Number of blocking parts',
  fix_cost_all_binding_children: 'Cost to unblock',
  contracted_lead_days: 'Lead time on contract',
  observed_mu_lead_days: 'Lead time actually seen',
  observed_sigma_lead_days: 'How much it varies',
  drift_days: 'Difference from contract',
  coefficient_of_variation: 'Unpredictability',
  otd_rate: 'Delivered on time',
  observations: 'Deliveries measured',
  affected_parts: 'Parts affected',
  annual_spend: 'Spend per year',
  ratio: 'Change vs recorded rate',
  cover_at_recorded_rate_days: 'Cover at the recorded rate',
  cover_at_corrected_rate_days: 'Cover at the real rate',
  safety_stock_shortfall_units: 'Short of safety stock by',
};

export function labelFor(field: string): string {
  const mapped = FIELD_LABELS[field];
  if (mapped) return mapped;
  const pretty = field.replace(/_/g, ' ');
  return pretty.charAt(0).toUpperCase() + pretty.slice(1);
}


// --- presenting the evidence ------------------------------------------------

// Fields whose value is a decimal probability. Shown as 5%, not 0.049 -- a reader scanning an
// audit table should not have to convert.
const PROBABILITY_FIELDS = new Set([
  'p_stockout', 'receiver_risk_before', 'receiver_risk_after',
  'donor_risk_before', 'donor_risk_after', 'otd_rate', 'reject_rate',
  'coefficient_of_variation',
]);

// Fields that are money. Everything else numeric stays as a plain count.
const MONEY_FIELDS = new Set([
  'unit_cost', 'effective_unit_cost', 'consequence', 'receiver_consequence', 'donor_consequence',
  'trapped_value', 'annual_carrying_cost', 'excess_holding_cost', 'subtotal', 'action_cost',
  'freight_cost', 'annual_spend', 'value_at_risk', 'parent_unit_cost',
  'fix_cost_all_binding_children',
]);

/** Money at the scale a person says it: Rs 2.96 crore, not Rs 29583117.84. */
function asMoney(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e7) return `Rs ${(value / 1e7).toFixed(2)} crore`;
  if (abs >= 1e5) return `Rs ${(value / 1e5).toFixed(2)} lakh`;
  return `Rs ${Math.round(value).toLocaleString('en-IN')}`;
}

/**
 * How one evidence row should read in the audit table.
 *
 * The table was a flat dump of whatever the scanner populated, so a live card showed
 * `Risk after the move 0.049`, `Cost if it runs out 29583117.84` and `Next best donor n/a`
 * with equal weight. Formatting by type and dropping the rows that carry no information is
 * most of the difference between an audit trail and wallpaper.
 */
export function presentValue(field: string, display: string, value: number | null): string {
  if (value === null) return display;
  const bare = field.includes('.') ? field.split('.').pop() ?? field : field;
  if (PROBABILITY_FIELDS.has(bare)) {
    const pct = value <= 1 ? value * 100 : value;
    return pct < 1 && pct > 0 ? `${pct.toFixed(1)}%` : `${Math.round(pct)}%`;
  }
  if (MONEY_FIELDS.has(bare)) return asMoney(value);
  if (bare.endsWith('_days') || bare.endsWith('_days_days')) return `${Math.round(value)} days`;
  if (bare.includes('per_day') || bare === 'forward_burn') {
    return `${value.toFixed(value < 10 ? 2 : 0)} a day`;
  }
  return Number.isInteger(value) ? value.toLocaleString('en-IN') : display;
}

/**
 * Rows worth showing. Drops the ones that were only ever populated because the field exists:
 * `Next best donor n/a`, `Freight 0`, `Warehouses considered 1`. Each one costs a line of
 * attention and answers nothing, and a live card carried three of them out of eighteen.
 *
 * A zero is only dropped where zero means "not applicable". A zero that is a real measurement --
 * no stock on hand, no forward burn on a dead part -- is the whole point of the row.
 */
const DROP_WHEN_EMPTY = new Set([
  'freight_cost', 'runner_up_donor', 'donors_considered', 'action_cost',
]);

export function worthShowing(f: CitableField): boolean {
  const bare = f.field.includes('.') ? f.field.split('.').pop() ?? f.field : f.field;
  const blank = f.display === 'n/a' || f.display === '' || f.display === '(none)';
  if (blank) return false;
  if (DROP_WHEN_EMPTY.has(bare) && (f.value === 0 || f.value === 1)) return false;
  return true;
}

// --- citation matching -----------------------------------------------------

export type CitableField = { ref: number; field: string; label: string; display: string; value: number | null };

// "Rs 1,525.59" / "7.23 crore" / "58" -> a number, so a rounded figure in the prose can be matched
// to the full-precision one in the evidence. Without the crore/lakh handling roughly half the
// citations would silently miss and legitimate figures would be flagged as invented.
export function toNumber(raw: string): number | null {
  const cleaned = raw.replace(/(?:Rs\.?|₹)/gi, '').replace(/,/g, '').trim();
  const m = cleaned.match(/^(-?\d+(?:\.\d+)?)\s*(crore|lakh)?$/i);
  if (!m) return null;
  let value = Number(m[1]);
  if (Number.isNaN(value)) return null;
  const scale = m[2]?.toLowerCase();
  if (scale === 'crore') value *= 1e7;
  if (scale === 'lakh') value *= 1e5;
  return value;
}

// Evidence lines are either "field: value" or "field: sub_a 1, sub_b 2" (a nested dict flattened
// by narration._evidence_lines). Both are citable, so the nested pairs are expanded too --
// otherwise every figure inside `purchase` reads as uncited.
export function citableFields(
  evidence: ParsedEvidence[],
  extras: { label: string; display: string; value: number | null }[] = [],
): CitableField[] {
  const fields: CitableField[] = [];
  let ref = 0;
  // Extras first, because the money at risk is the figure most likely to appear in the prose and
  // it is carried on the DECISION VALUE line rather than in the evidence block. Without it the
  // headline number on every card rendered as unsupported.
  for (const extra of extras) {
    fields.push({ ref: ++ref, field: extra.label, label: extra.label, display: extra.display, value: extra.value });
  }
  for (const row of evidence) {
    const nested = [...row.finding.matchAll(/([a-z_][a-z0-9_]*)\s+((?:Rs\.?\s*)?-?[\d,]+(?:\.\d+)?)/gi)];
    const looksNested = nested.length >= 2;
    if (looksNested) {
      for (const n of nested) {
        fields.push({
          ref: ++ref, field: `${row.source}.${n[1]}`, label: labelFor(n[1]),
          display: n[2].trim(), value: toNumber(n[2]),
        });
      }
    } else {
      fields.push({
        ref: ++ref, field: row.source, label: labelFor(row.source),
        display: row.finding, value: toNumber(row.finding),
      });
    }
  }
  return fields;
}

export type Segment = { text: string; ref: number | null; uncited: boolean; title?: string };

// The leading guard matters more than it looks: without it the digits inside an identifier are
// read as a figure, so "WH001" picked up a citation for 001 and "P0009" one for 0009. A number
// that is part of a word is never a measurement.
const FIGURE_RE = /(?<![A-Za-z0-9])(?:Rs\.?\s*|₹\s*)?\d[\d,]*(?:\.\d+)?(?:\s*(?:crore|lakh))?/gi;

// Numbers this small are usually ordinals or counts in a sentence ("one or two", "2 of 4") rather
// than measurements, and flagging them produces noise that trains the reader to ignore the flag.
const MIN_FLAGGABLE = 3;

export function citeProse(prose: string, fields: CitableField[]): Segment[] {
  const segments: Segment[] = [];
  let cursor = 0;
  for (const match of prose.matchAll(FIGURE_RE)) {
    const start = match.index ?? 0;
    const raw = match[0];
    const value = toNumber(raw);
    if (value === null) continue;

    // An ordinal is a word, not a measurement. "90th percentile" was flagged as an unsupported
    // figure while the number it describes -- 37 days -- cited correctly right beside it.
    if (/^(?:st|nd|rd|th)\b/i.test(prose.slice(start + raw.length))) continue;

    if (start > cursor) segments.push({ text: prose.slice(cursor, start), ref: null, uncited: false });

    // Two ways to match, and both are needed.
    //
    // Precision: prose rounds. "23 days of cover" is the correct rendering of a real 22.6, and
    // on relative error alone that is 1.7% out and would be flagged as invented -- punishing the
    // model for writing readably. So a figure written to N decimals matches any measurement that
    // rounds to it at N decimals.
    //
    // Relative: "Rs 7.23 crore" against 72324000 is 0.03% out but not a rounding of it at any
    // decimal place, because the scale word moves the precision. That needs a tolerance.
    const scaled = /crore|lakh/i.test(raw);
    const decimals = scaled ? 0 : (raw.split('.')[1]?.replace(/\D/g, '').length ?? 0);
    const factor = 10 ** decimals;

    // A figure written as a percentage is stored as a fraction: receiver_risk_before is 0.76 and
    // the prose says 76%. Without this the matcher sees a 99% discrepancy and flags a perfectly
    // sound number as invented -- which it did, on the two risk figures of every transfer.
    const isPercent = /^\s*%/.test(prose.slice(start + raw.length));

    let best: CitableField | null = null;
    let bestError = Infinity;
    for (const candidate of fields) {
      const raw = candidate.value;
      if (raw === null) continue;
      // Compare against the percentage form when the prose wrote one, but keep `candidate` as
      // the thing cited so the reader is shown the measurement as recorded.
      const f = { value: isPercent && Math.abs(raw) <= 1 ? raw * 100 : raw };
      // A half-unit window at the printed precision, not an exact round-trip. "25%" is a fair
      // rendering of 25.5, and so is "26%" -- rounding up, rounding down and truncating are all
      // legitimate, and requiring one of them flagged the other two as invented.
      if (!scaled && Math.abs(f.value - value) <= 0.5 / factor) {
        best = candidate;
        bestError = 0;
        break;
      }
      const magnitude = Math.max(Math.abs(f.value), Math.abs(value), 1);
      const error = Math.abs(f.value - value) / magnitude;
      if (error < bestError) { bestError = error; best = candidate; }
    }

    if (best && bestError <= 0.01) {
      segments.push({ text: raw, ref: best.ref, uncited: false, title: `${best.label}: ${best.display}` });
    } else {
      segments.push({
        text: raw, ref: null,
        uncited: Math.abs(value) >= MIN_FLAGGABLE,
        title: 'No measurement behind this figure — check it before acting on it',
      });
    }
    cursor = start + raw.length;
  }
  if (cursor < prose.length) segments.push({ text: prose.slice(cursor), ref: null, uncited: false });
  return segments;
}

// --- small pieces ----------------------------------------------------------

export type Verdict = {
  label: string;
  tone: 'destructive' | 'warning' | 'neutral';
  /** Categorical accent for this action type. Identity, not severity. */
  accent: string;
};

const ACTION_ACCENT: Record<string, string> = {
  TRANSFER: 'chart-cat-1',
  PURCHASE: 'chart-cat-2',
  RECALIBRATE: 'chart-cat-3',
  RENEGOTIATE: 'chart-cat-7',
  RESOURCE: 'chart-cat-8',
  REVIEW_STOCK: 'chart-cat-4',
  EXPEDITE: 'chart-cat-5',
  NONE: 'chart-cat-6',
};

const ACTION_TYPE_LABELS: Record<string, string> = {
  TRANSFER: 'Move stock',
  PURCHASE: 'Buy',
  REVIEW_STOCK: 'Review',
  RECALIBRATE: 'Adjust plan',
  RENEGOTIATE: 'Renegotiate',
  RESOURCE: 'Re-source',
  EXPEDITE: 'Expedite',
  NONE: 'Action',
};

export function classifyRecommendation(recommendation: string | null, actionType?: string | null): Verdict {
  const text = (recommendation ?? '').toUpperCase();

  // Prefer the recorded action type. Reading the verb out of the prose works for "transfer 660
  // units ..." but not for "SUP018: consistently late ...", which has no verb at the front and
  // fell through to a generic label while the line itself said RECALIBRATE all along.
  if (actionType && ACTION_TYPE_LABELS[actionType] && !text.startsWith('VERIFY DATA') && !text.startsWith('ESCALATE')) {
    return { label: ACTION_TYPE_LABELS[actionType], tone: 'neutral', accent: ACTION_ACCENT[actionType] ?? 'chart-cat-6' };
  }

  // Colour carries SEVERITY here, not category. An earlier pass gave each of the action types its
  // own hue -- move/buy/review/adjust in four colours -- which produced a rainbow that a reader
  // has to learn before it tells them anything, while the label beside it already said the same
  // word. Only the states that mean "something is wrong" are coloured; the rest are neutral, so
  // when colour does appear it means something.
  if (text.startsWith('VERIFY DATA')) return { label: 'Data anomaly', tone: 'destructive', accent: 'destructive' };
  if (text.startsWith('ESCALATE')) return { label: 'Escalate', tone: 'destructive', accent: 'destructive' };
  if (text.startsWith('EXPEDITE')) return { label: 'Stalled', tone: 'warning', accent: 'chart-cat-5' };
  if (/^TRANSFER|^MOVE/.test(text)) return { label: 'Move stock', tone: 'neutral', accent: 'chart-cat-1' };
  if (/^BUY|^PURCHASE|^ORDER/.test(text)) return { label: 'Buy', tone: 'neutral', accent: 'chart-cat-2' };
  if (/^REVIEW/.test(text)) return { label: 'Review', tone: 'neutral', accent: 'chart-cat-4' };
  if (/^RAISE|^RECALIBRATE|^LOWER|^UPDATE/.test(text)) return { label: 'Adjust plan', tone: 'neutral', accent: 'chart-cat-3' };
  return { label: 'Action', tone: 'neutral', accent: 'chart-cat-6' };
}

export type Assumption = { name: string; value: string; kind: string | null; basis: string | null };

// "holding_rate = 14%/yr (policy: ...)". The `kind` matters most: a measured input is arguable
// against the data, a policy one is a choice someone made. Anything not matching the shape (older
// quotes, whose ASSUMPTIONS line was free prose) yields [] and falls back to the raw line.
export function parseAssumptions(line: string | null): Assumption[] {
  if (!line) return [];
  const parsed: Assumption[] = [];
  for (const part of line.split(/;\s*(?=[A-Za-z_][A-Za-z0-9_]*\s*=)/)) {
    const m = part.trim().match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)\s*\(([A-Za-z ]+):\s*(.+)\)$/);
    if (!m) return [];
    parsed.push({ name: m[1], value: m[2].trim(), kind: m[3], basis: m[4] });
  }
  return parsed;
}

// Both markers. The brief says "## ACTION ITEM i of N" since the layout rework; every quote
// written before it says "## CANDIDATE". A report with neither is one block, so the oldest quotes
// render exactly as they always did.
const ITEM_MARKER_RE = /^##\s*(?:ACTION ITEM|CANDIDATE)\s+(\d+)\s+of\s+(\d+).*$/gm;

export function splitIntoBlocks(text: string): string[] {
  const markers = [...text.matchAll(ITEM_MARKER_RE)];
  if (markers.length === 0) return [text];
  const blocks: string[] = [];
  for (let i = 0; i < markers.length; i++) {
    const start = markers[i].index ?? 0;
    const end = i + 1 < markers.length ? (markers[i + 1].index ?? text.length) : text.length;
    blocks.push(text.slice(start, end).trim());
  }
  return blocks;
}
