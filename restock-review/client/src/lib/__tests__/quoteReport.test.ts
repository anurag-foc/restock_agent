import { describe, expect, it } from 'vitest';
import {
  citableFields, citeProse, parseSummaryReport, splitIntoBlocks, toNumber, labelFor,
  presentValue,
  worthShowing,
} from '../quoteReport';

// A real stored summary_report, taken verbatim from quote_metadata. Synthetic fixtures would
// agree with whatever the parser happens to do; this one was written by the model.
const REAL_REPORT = '## CANDIDATE 1 of 4\n\nRECOMMENDATION: transfer 2135 units of P0034 from WH002 to WH001\nWHY NOW: WH001 has 1406 units available against a need of 2135 units, leaving a receiver risk of 1.0 before the transfer. The donor warehouse WH002 will retain 91 days of cover after releasing the stock.\nOPTIONS CONSIDERED:\n  [CHOSEN] TRANSFER: transfer 2135 units of P0034 from WH002 to WH001 — cost the donor is left with 91 days of cover from 9988 units\n  [NOT CHOSEN] no other option was available for this subject\nEVIDENCE:\n  transfer_qty: 2135\n  donor_warehouse_id: WH002\n  receiver_warehouse_id: WH001\n  receiver_available: 1406\n  receiver_need: 2135\n  donor_available: 9988\n  donor_safety_stock: 2084\n  donor_burn_per_day: 86.29\n  donor_cover_after_days: 91.0\n  freight_cost: 0.0\n  donors_considered: 1\n  runner_up_donor: None\n  receiver_risk_before: 1.0\n  receiver_risk_after: 0.01\n  donor_risk_before: 0.001\n  donor_risk_after: 0.016\nIF APPROVED AND WRONG: the donor is left with 91 days of cover from 9988 units\nDECISION VALUE: Rs 14,64,59,335 (14.65 crore) at risk\nASSUMPTIONS USED: service_level = 99% (A-CRITICAL) (policy: tiered by criticality class)';

describe('toNumber', () => {
  it('reads Indian-grouped rupees', () => expect(toNumber('Rs 19,22,239')).toBe(1922239));
  it('scales crore and lakh so a rounded figure can match a precise one', () => {
    expect(toNumber('7.23 crore')).toBe(72300000);
    expect(toNumber('19.22 lakh')).toBe(1922000);
  });
  it('returns null for things that are not figures', () => {
    expect(toNumber('WH002')).toBeNull();
    expect(toNumber('None')).toBeNull();
  });
});

describe('citation matching on a real report', () => {
  const parsed = parseSummaryReport(REAL_REPORT);
  const fields = citableFields(parsed.evidence);

  it('parses the report the model actually wrote', () => {
    expect(parsed.recommendation).toContain('transfer 2135 units of P0034');
    expect(parsed.whyNow).toBeTruthy();
    expect(fields.length).toBeGreaterThan(5);
  });

  it('cites every figure in WHY NOW back to a measurement', () => {
    const segments = citeProse(parsed.whyNow ?? '', fields);
    const uncited = segments.filter((s) => s.uncited);
    // The prose is required to use only printed figures, so anything unmatched is either a
    // fabrication or a matching bug -- both worth failing over.
    expect(uncited, JSON.stringify(uncited)).toHaveLength(0);
    expect(segments.some((s) => s.ref !== null)).toBe(true);
  });

  it('flags a figure that has no measurement behind it', () => {
    // Exactly the shape of the fabrications on record: a plausible rupee figure, back-solved,
    // matching nothing in the evidence.
    const segments = citeProse('This will cost Rs 17,280 in handling.', fields);
    expect(segments.some((s) => s.uncited)).toBe(true);
  });

  it('does not flag small counts, which are usually words not measurements', () => {
    const segments = citeProse('There is 1 donor and 2 options.', fields);
    expect(segments.filter((s) => s.uncited)).toHaveLength(0);
  });

  it('expands nested evidence so figures inside a dict are citable', () => {
    const nested = citableFields([{ source: 'purchase', finding: 'moq 60, pack_size 30, subtotal 1922238.75' }]);
    expect(nested.map((f) => f.field)).toContain('purchase.moq');
    expect(nested.find((f) => f.field === 'purchase.subtotal')?.value).toBe(1922238.75);
  });
});

describe('backwards compatibility', () => {
  it('splits on both the old and new item markers', () => {
    expect(splitIntoBlocks('## CANDIDATE 1 of 2\na\n## CANDIDATE 2 of 2\nb')).toHaveLength(2);
    expect(splitIntoBlocks('## ACTION ITEM 1 of 2\na\n## ACTION ITEM 2 of 2\nb')).toHaveLength(2);
  });

  it('treats a report with no marker as a single item', () => {
    expect(splitIntoBlocks('RECOMMENDATION: do a thing')).toHaveLength(1);
  });
});

describe('field labels', () => {
  it('translates the ones a planner would not recognise', () => {
    expect(labelFor('p_stockout')).toBe('Chance of running out');
    expect(labelFor('excess_holding_cost')).toBe('Cost of holding the extra');
  });
  it('prettifies anything unmapped rather than showing snake_case', () => {
    expect(labelFor('some_new_field')).toBe('Some new field');
  });
});


// A second real report, written after the transfer shortfall field was renamed from
// receiver_need to receiver_short_by. That rename exists because the old name made the model
// write "WH002 has 3676 units against a need of 1755" -- which reads as comfortably stocked and
// makes the transfer look pointless. The evidence field is the only thing the model sees, so the
// field had to say what it means.
const REPORT_AFTER_RENAME = "## ACTION ITEM 1 of 4\n\nRECOMMENDATION: transfer 829 units of P0037 from WH002 to WH001\nWHY NOW: WH001 is short by 829 units while WH002 holds 3673 units against a safety stock of 1502, and the transfer leaves the donor with 23 days of cover at its burn rate of 126 units per day.\nOPTIONS CONSIDERED:\n  [CHOSEN] TRANSFER: transfer 829 units of P0037 from WH002 to WH001 \u2014 cost the donor is left with 23 days of cover from 3673 units\n  [NOT CHOSEN] no other option was available for this subject\nEVIDENCE:\n  transfer_qty: 829\n  donor_warehouse_id: WH002\n  receiver_warehouse_id: WH001\n  receiver_available: 1142\n  receiver_short_by: 829\n  donor_available: 3673\n  donor_safety_stock: 1502\n  donor_burn_per_day: 126.02\n  donor_cover_after_days: 22.6\n  freight_cost: 0.0\n  donors_considered: 1\n  runner_up_donor: None\n  receiver_risk_before: 0.99\n  receiver_risk_after: 0.01\n  donor_risk_before: 0.595\n  donor_risk_after: 0.982\nIF APPROVED AND WRONG: the donor is left with 23 days of cover from 3673 units\nDECISION VALUE: Rs 4,77,43,187 (4.77 crore) at risk\nASSUMPTIONS USED: service_level = 99% (A-CRITICAL) (policy: tiered by criticality class)";

describe('the renamed shortfall field', () => {
  const parsed = parseSummaryReport(REPORT_AFTER_RENAME);
  const fields = citableFields(parsed.evidence);

  it('still cites every figure in the prose', () => {
    const uncited = citeProse(parsed.whyNow ?? '', fields).filter((s) => s.uncited);
    expect(uncited, JSON.stringify(uncited)).toHaveLength(0);
  });

  it('labels the shortfall as a gap, not a requirement', () => {
    expect(labelFor('receiver_short_by')).toBe('Receiving site short by');
  });

  it('uses the new ACTION ITEM marker', () => {
    expect(REPORT_AFTER_RENAME.startsWith('## ACTION ITEM')).toBe(true);
  });
});

describe('rounding is not fabrication', () => {
  const fields = citableFields([
    { source: 'donor_cover_after_days', finding: '22.6' },
    { source: 'consequence', finding: '72324000.0' },
  ]);

  it('accepts a figure the model correctly rounded', () => {
    // A live report wrote "23 days of cover" for a real 22.6. On relative error that is 1.7% out
    // and was flagged as invented -- punishing the model for writing readably.
    const segments = citeProse('leaves the donor with 23 days of cover', fields);
    expect(segments.filter((s) => s.uncited)).toHaveLength(0);
    expect(segments.find((s) => s.text === '23')?.title).toContain('22.6');
  });

  it('accepts a scaled figure whose precision the scale word moved', () => {
    const segments = citeProse('Rs 7.23 crore of production stops', fields);
    expect(segments.filter((s) => s.uncited)).toHaveLength(0);
  });

  it('still rejects a figure that is not a rounding of anything', () => {
    const segments = citeProse('freight of Rs 17,280', fields);
    expect(segments.some((s) => s.uncited)).toBe(true);
  });
});

describe('figures inside identifiers are not measurements', () => {
  const fields = citableFields([{ source: 'transfer_qty', finding: '660' }]);

  it('does not cite the digits in a part or warehouse id', () => {
    // "transferring 660 units from WH001" rendered WH001 with a citation for 001, and P0009 with
    // one for 0009 -- every identifier on the card picked up a spurious reference.
    const segments = citeProse('move 660 units of P0009 from WH001 to WH002', fields);
    const marked = segments.filter((s) => s.ref !== null || s.uncited);
    expect(marked).toHaveLength(1);
    expect(marked[0].text).toBe('660');
  });
});

describe('the money at risk is citable', () => {
  it('does not flag the headline figure, which lives on DECISION VALUE not in EVIDENCE', () => {
    const fields = citableFields(
      [{ source: 'transfer_qty', finding: '660' }],
      [{ label: 'Money at risk', display: 'Rs 6,11,97,102', value: 61197102 }],
    );
    const segments = citeProse('placing Rs 6,11,97,102 at risk', fields);
    expect(segments.filter((s) => s.uncited)).toHaveLength(0);
    expect(segments.some((s) => s.ref === 1)).toBe(true);
  });
});

describe('things that look like figures but are not', () => {
  const fields = citableFields([{ source: 'p90_lead_days', finding: '37.3' }]);

  it('does not flag an ordinal', () => {
    // "90th percentile at 37 days" flagged the 90 while correctly citing the 37 beside it.
    const segments = citeProse('90th percentile at 37 days', fields);
    expect(segments.filter((s) => s.uncited)).toHaveLength(0);
  });
});

describe('the single-figure decision value shape', () => {
  it('yields an exposure, so the headline money is citable', () => {
    // A costless action prints one figure because decision value and exposure are equal. Parsing
    // only decisionValue left exposure null and the biggest number on the card unsupported.
    const parsed = parseSummaryReport(
      'RECOMMENDATION: do a thing\nDECISION VALUE: Rs 28,12,325 (28.12 lakh) at risk',
    );
    expect(parsed.exposure).toBe('28,12,325');
    expect(parsed.decisionValue).toBe('28,12,325');
  });
});

describe('percentages are stored as fractions', () => {
  const fields = citableFields([
    { source: 'receiver_risk_before', finding: '0.76' },
    { source: 'receiver_risk_after', finding: '0.25' },
  ]);

  it('matches a percentage in the prose to the fraction behind it', () => {
    // Every transfer's two risk figures were flagged as invented: the prose says 76% and the
    // measurement is 0.76, which on relative error is a 99% discrepancy.
    const segments = citeProse('receiver risk stands at 76%; moving drops it to 25%', fields);
    expect(segments.filter((s) => s.uncited)).toHaveLength(0);
  });

  it('does not treat a bare number as a percentage', () => {
    const segments = citeProse('there are 76 units', fields);
    expect(segments.some((s) => s.uncited)).toBe(true);
  });
});

describe('rounding direction is not the model\'s problem', () => {
  const fields = citableFields([{ source: 'receiver_risk_after', finding: '0.255' }]);

  it('accepts a percentage rounded either way', () => {
    // 0.255 is 25.5%. "25%" and "26%" are both fair renderings; requiring one flagged the other.
    expect(citeProse('drops to 25%', fields).filter((s) => s.uncited)).toHaveLength(0);
    expect(citeProse('drops to 26%', fields).filter((s) => s.uncited)).toHaveLength(0);
  });

  it('still rejects a figure outside the rounding window', () => {
    expect(citeProse('drops to 31%', fields).some((s) => s.uncited)).toBe(true);
  });
});

describe('the two-figure decision value shape', () => {
  it('reads the exposure past the scale parenthetical', () => {
    // "Rs 6,19,414 (6.19 lakh) (Rs 17,84,154 (17.84 lakh) at risk, ...)". The old pattern could
    // not match across "(17.84 lakh)", fell back to the first figure, and every costed finding
    // showed its decision value under an "at risk" label.
    const parsed = parseSummaryReport(
      'RECOMMENDATION: buy\nDECISION VALUE: Rs 6,19,414 (6.19 lakh) (Rs 17,84,154 (17.84 lakh) at risk, ranked after allowing for how expensive the cheapest fix is)',
    );
    expect(parsed.decisionValue).toBe('6,19,414');
    expect(parsed.exposure).toBe('17,84,154');
  });
});

describe('what the headline figure IS, not just how big it is', () => {
  it('keeps a transfer labelled as a recovery', () => {
    // FX1's `benefit` is risk removed at the receiver already net of risk created at the donor.
    // The card hardcoded "at risk" under every figure, so a live transfer read
    // "Rs 3,10,09,798 at risk" where Rs 3.62 crore was at risk and Rs 3.10 crore was recovered.
    const parsed = parseSummaryReport(
      'RECOMMENDATION: transfer 4236 units\nDECISION VALUE: Rs 3,10,09,798 (3.10 crore) of risk removed',
    );
    expect(parsed.exposure).toBe('3,10,09,798');
    expect(parsed.exposureLabel).toBe('of risk removed');
  });

  it('keeps dead capital labelled as a recurring cost', () => {
    // An annual carrying cost rendered as "at risk" reads as an imminent loss.
    const parsed = parseSummaryReport(
      'RECOMMENDATION: write down\nDECISION VALUE: Rs 7,12,00,000 (7.12 crore) a year to hold',
    );
    expect(parsed.exposureLabel).toBe('a year to hold');
  });

  it('still says at risk for everything else', () => {
    const parsed = parseSummaryReport(
      'RECOMMENDATION: buy\nDECISION VALUE: Rs 6,19,414 (6.19 lakh) (Rs 17,84,154 (17.84 lakh) at risk, ranked after allowing for how expensive the cheapest fix is)',
    );
    expect(parsed.exposureLabel).toBe('at risk');
  });

  it('falls back to at risk on a quote written before the distinction existed', () => {
    const parsed = parseSummaryReport('RECOMMENDATION: buy\nDECISION VALUE: Rs 28,12,325 at risk');
    expect(parsed.exposureLabel).toBe('at risk');
  });
});

// --- the audit table: formatting and dropping dead rows ---------------------
//
// Every value below is from the live quote QT-20260914-F3BFB1, whose table rendered
// `Risk after the move 0.049`, `Cost if it runs out 29583117.84` and `Next best donor n/a`
// with equal weight.

describe('presentValue', () => {
  it('shows probabilities as percentages, not decimals', () => {
    expect(presentValue('receiver_risk_after', '0.049', 0.049)).toBe('5%');
    expect(presentValue('receiver_risk_before', '1.0', 1.0)).toBe('100%');
  });

  it('keeps a sub-1% risk visible instead of rounding it to zero', () => {
    // "0%" next to a live finding reads as "there is no risk", which is the opposite of true.
    expect(presentValue('p_stockout', '0.004', 0.004)).toBe('0.4%');
  });

  it('shows money at the scale a person says it', () => {
    expect(presentValue('receiver_consequence', '29583117.84', 29583117.84)).toBe('Rs 2.96 crore');
    expect(presentValue('annual_carrying_cost', '1170243.2', 1170243.2)).toBe('Rs 11.70 lakh');
    expect(presentValue('unit_cost', '1174', 1174)).toBe('Rs 1,174');
  });

  it('labels rates and durations with their unit', () => {
    expect(presentValue('donor_burn_per_day', '100.77', 100.77)).toBe('101 a day');
    expect(presentValue('donor_cover_after_days', '48.7', 48.7)).toBe('49 days');
  });

  it('leaves non-numeric values exactly as they were', () => {
    expect(presentValue('burn_method', 'LEVEL_X_SEASON', null)).toBe('LEVEL_X_SEASON');
  });
});

describe('worthShowing', () => {
  const row = (field: string, display: string, value: number | null) => ({
    ref: 1, field, label: field, display, value,
  });

  it('drops rows that exist only because the field exists', () => {
    expect(worthShowing(row('runner_up_donor', 'n/a', null))).toBe(false);
    expect(worthShowing(row('freight_cost', '0.0', 0))).toBe(false);
    expect(worthShowing(row('donors_considered', '1', 1))).toBe(false);
  });

  it('keeps a zero that is a real measurement', () => {
    // A dead part genuinely burns nothing, and that zero is the entire finding.
    expect(worthShowing(row('forward_burn', '0.0', 0))).toBe(true);
    expect(worthShowing(row('on_hand_qty', '0', 0))).toBe(true);
  });

  it('keeps ordinary rows', () => {
    expect(worthShowing(row('transfer_qty', '2582', 2582))).toBe(true);
  });
});
