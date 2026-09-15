/**
 * The settings registry, mirrored for the UI and the write endpoint.
 *
 * **`src/agentic_restock/settings.py` is authoritative.** The pipeline reads that one;
 * this exists because the browser and the Node server cannot. The two must change
 * together, and `tests/test_settings_spec_parity.py` fails the Python suite if they
 * drift -- a key, default or bound that disagrees would let a client set something the
 * pipeline then silently ignores, which is the hardest kind of bug to be told about.
 *
 * Labels and help text live only here. They are the client-facing half and have no
 * business in the pipeline.
 */

export type SettingKind = 'float' | 'int' | 'choice';

export type SettingChoice = {
  value: string;
  label: string;
  /** Shown under the option when it is selected. Plain language, no statistics. */
  help?: string;
};

export type SettingSpec = {
  key: string;
  kind: SettingKind;
  default: number | string;
  label: string;
  /** One line under the control saying what changing it does. */
  help?: string;
  min?: number;
  max?: number;
  choices?: SettingChoice[];
  /** Legal, but the panel should say something. */
  warnAbove?: number;
  /** Rendered as a percentage: stored 0.14, shown as 14. */
  asPercent?: boolean;
  /** Unit shown beside the input. Declared rather than guessed from the key name --
   *  `key.includes('value')` silently attaches a rupee sign to the wrong field the
   *  moment someone adds a setting whose name happens to contain it. */
  unit?: string;
  /** Decimal places to show. Without it, 0.14 * 100 renders as 14.000000000000002. */
  decimals?: number;
  /** Which action types move when this changes. */
  affects?: string[];
};

export const SETTINGS: SettingSpec[] = [
  {
    key: 'consumption_model',
    kind: 'choice',
    default: 'automatic',
    label: 'How fast are parts being used up?',
    help: 'We look at what you have actually issued from the shelf over the past three years, and work out how fast each part is going.',
    affects: ['Stockout risk', 'Transfers', 'Order quantities', 'Dead stock'],
    choices: [
      {
        value: 'automatic',
        label: 'Work it out for each part (recommended)',
        help: 'Parts behave differently. A bolt goes out every day. A spare pump sits still for months, then goes out all at once. Averaging those the same way gets both wrong. We look at each part on its own and handle it the right way — there is nothing for you to set up, and we redo it every time we check.',
      },
      {
        value: 'simple_average',
        label: 'Just take a plain average',
        help: 'Add up everything used, divide by the number of days. This is what most ERP systems do. Choose it if you want to compare our numbers against the ones you already have.',
      },
      {
        value: 'recent_only',
        label: 'Only count the last few months',
        help: 'Throw away older history. Useful when something real has changed — a new model, a new plant, a drop in orders — and last year no longer tells you anything. Careful: a part that only moves once or twice a year will look like it has stopped completely.',
      },
    ],
  },
  {
    key: 'consumption_recent_days',
    kind: 'int',
    default: 90,
    label: 'How far back should we look?',
    min: 30,
    max: 365,
    decimals: 0,
    unit: 'days',
  },
  {
    key: 'leadtime_model',
    kind: 'choice',
    default: 'recent_weighted',
    label: 'How long do your suppliers really take?',
    help: 'The contract says one thing. Actual deliveries say another. We go by what actually arrives, and by how much it varies — a supplier who is always 30 days is easy to plan around; one who is sometimes 20 and sometimes 50 is not.',
    affects: ['Stockout risk', 'Supplier alerts', 'Re-sourcing'],
    choices: [
      {
        value: 'recent_weighted',
        label: 'Go mostly by recent deliveries (recommended)',
        help: 'A supplier who was bad last year but has been on time for six months is judged on the six months. Fair to suppliers who have fixed their problems, and quick to notice one who is slipping.',
      },
      {
        value: 'ignore_outliers',
        label: 'Ignore the odd bad delivery',
        help: 'One shipment stuck at the port will not wreck a supplier\'s record. The trade-off: if a supplier is genuinely getting worse, we will be slower to say so.',
      },
    ],
  },
  {
    key: 'leadtime_recency',
    kind: 'choice',
    default: 'normal',
    label: 'How quickly should old deliveries stop counting?',
    help: 'Fast means only the last few months really matter. Slow means a supplier carries their whole history with them.',
    choices: [
      { value: 'fast', label: 'Fast' },
      { value: 'normal', label: 'Normal' },
      { value: 'slow', label: 'Slow' },
    ],
  },
  {
    key: 'holding_rate',
    kind: 'float',
    default: 0.14,
    label: 'What does it cost you to keep stock sitting for a year?',
    help: 'Stock on a shelf is not free — it is cash you cannot spend, space you are paying for, insurance, and parts that go out of date before you use them. If you are not sure, leave it at 14%; that is typical for manufacturing. Put it up and we will push harder to clear slow stock and argue harder against big minimum orders.',
    min: 0.05,
    max: 0.4,
    decimals: 1,
    unit: '% a year',
    asPercent: true,
    affects: ['Dead stock', 'Minimum orders', 'Supplier comparison', 'Supplier alerts'],
  },
  {
    key: 'transfer_caution',
    kind: 'choice',
    default: 'balanced',
    label: 'When we move stock between warehouses, how much should the one giving it away keep back?',
    help: 'Often the cheapest fix is not buying more — it is moving stock you already own from a warehouse that has plenty to one that is short. No purchase order, no waiting for a supplier. But the warehouse lending it must not be left short itself.',
    affects: ['Transfers'],
    choices: [
      {
        value: 'relaxed',
        label: 'Lend freely',
        help: 'More stock gets moved, and more shortages get solved without buying anything. The warehouse lending may be left with little to spare.',
      },
      {
        value: 'balanced',
        label: 'Keep a safe buffer back (recommended)',
        help: 'A warehouse lends only what it has genuinely spare, and always keeps about three weeks of its own supply.',
      },
      {
        value: 'protective',
        label: 'Lend only what is clearly spare',
        help: 'Fewer and smaller moves, so you will buy more often. In exchange, a warehouse that lends is never left short.',
      },
    ],
  },
  {
    key: 'dead_stock_cover_days',
    kind: 'float',
    default: 180,
    label: 'When should we call stock "not moving"?',
    help: 'At 180 days we tell you when you are holding about six months of supply. Lower it and we will flag more stock as idle; raise it and we will leave more alone.',
    min: 30,
    max: 730,
    decimals: 0,
    unit: 'days',
    affects: ['Dead stock'],
  },
  {
    key: 'dead_stock_min_value',
    kind: 'float',
    default: 50000,
    label: '...and only if it is worth at least',
    help: 'Both have to be true before we mention it, so you are not troubled about a shelf of cheap washers.',
    min: 1000,
    decimals: 0,
    unit: '\u20b9',
    affects: ['Dead stock'],
  },
  {
    key: 'items_per_notification',
    kind: 'int',
    default: 4,
    label: 'How many things should we send you at a time?',
    help: 'We check twice a day and send only the most important. A short list gets acted on; a long one gets ignored — and a system nobody reads is the problem this is meant to solve.',
    min: 1,
    max: 10,
    warnAbove: 6,
  },
  {
    key: 'min_exposure',
    kind: 'float',
    default: 25000,
    label: 'Do not tell us about anything worth less than',
    help: 'Small problems stay out of your way entirely.',
    min: 1000,
    decimals: 0,
    unit: '\u20b9',
  },
];

export const SETTINGS_BY_KEY: Record<string, SettingSpec> = Object.fromEntries(
  SETTINGS.map((s) => [s.key, s]),
);

/** Parse and bounds-check one value. Mirrors `settings.coerce` in Python. */
export function coerceSetting(key: string, value: unknown): number | string {
  const spec = SETTINGS_BY_KEY[key];
  if (!spec) throw new Error(`unknown setting ${key}`);

  if (spec.kind === 'choice') {
    const text = String(value);
    if (!spec.choices?.some((c) => c.value === text)) {
      throw new Error(`${key}: ${text} is not one of ${spec.choices?.map((c) => c.value).join(', ')}`);
    }
    return text;
  }

  const parsed = spec.kind === 'int' ? Number.parseInt(String(value), 10) : Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${key}: ${String(value)} is not a ${spec.kind}`);
  if (spec.min !== undefined && parsed < spec.min) throw new Error(`${key}: ${parsed} is below the minimum of ${spec.min}`);
  if (spec.max !== undefined && parsed > spec.max) throw new Error(`${key}: ${parsed} is above the maximum of ${spec.max}`);
  return parsed;
}
