import { useState } from 'react';
import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
import {
  Alert,
  AlertDescription,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Checkbox,
  Input,
  Label,
  Skeleton,
} from '@databricks/appkit-ui/react';
import { SETTINGS_BY_KEY } from '../../../shared/settingsSpec';

/**
 * Written for a client watching a demo, not for us.
 *
 * One page, one chart, one message: your ERP has one test ("buy more of this part"); this
 * system runs eight, on the same stock and suppliers, on the same day.
 *
 * Two decisions shape everything below, both made after an earlier draft of this page tried to
 * do more and confused more than it convinced:
 *
 * - **The ERP bar and our eight bars are visually separated, not sorted into one ranked list.**
 *   Interleaving them would bury the actual claim -- there is only one kind of answer on their
 *   side -- as a fact a viewer would have to notice rather than the shape of the chart itself.
 * - **A count is a claim nobody can check, so every one of our eight bars expands into a real
 *   worked example** -- the actual part or supplier, and the numbers that specific kind of
 *   problem reasons from (stock on hand and lead time for a shortage; two warehouses' cover for
 *   a transfer; a quoted price against a reject rate for a supplier switch). Generated once by
 *   `simulation/reasoning.py` from the finding's own evidence and stored, not invented here.
 *
 * A second chart (how much of what it finds actually reaches a person) was designed and shelved
 * for a later pass -- naively placed next to this one, a smaller bar for "shown" reads as
 * "worse," when it is the opposite: restraint, not a smaller result.
 */

type SimTypeSummary = {
  RUN_ID: string;
  BATCH_ID: string;
  RUN_TS: string;
  LABEL: string;
  CONSUMPTION_MODEL: string;
  FINDING_TYPE: string;
  COUNT: number;
  EXAMPLE_SUBJECT: string;
  EXAMPLE_REASONING: string;
  EXAMPLE_EXPOSURE: number;
  /** Plain-English hit rate against ground truth. Empty for the five types with no ground
   *  truth to check against yet -- see simulation/reasoning.py::_accuracy_note. */
  ACCURACY_NOTE: string;
};

type SimBenchmarkCheck = {
  BATCH_ID: string;
  RUN_TS: string;
  LABEL: string;
  FINDING_ID: string;
  NAME: string;
  PROVES: string;
  FINDING_TYPE: string;
  IS_NEGATIVE: boolean;
  RESULT: string;
  SUBJECT: string;
  DETAIL: string;
  SHOWN_TO_PM: boolean;
};

const ENGINE_CHOICES = SETTINGS_BY_KEY['consumption_model'].choices ?? [];
const BUDGET_SPEC = SETTINGS_BY_KEY['items_per_notification'];

/** Mirrors `simulation/reasoning.py::PLAIN_NAME`. The chart never shows the internal constant
 *  (REDEPLOYMENT, MOQ_UNECONOMIC, ...) -- this is the one place both sides agree on the words. */
const KIND_NAMES: Record<string, string> = {
  STOCKOUT_RISK: 'Running out',
  CASCADE_BLOCK: 'Assembly line blocked',
  REDEPLOYMENT: 'Move stock instead of buying',
  DEAD_CAPITAL: 'Stock that will never move',
  LEADTIME_SIGNAL: 'Supplier slipping',
  DEMAND_SHIFT: "Demand changed, buffer didn't",
  SUPPLIER_ECONOMICS: 'Cheaper supplier available',
  MOQ_UNECONOMIC: 'Bad order size',
};

/** Plain-language name for each of the eleven planted problems, matching the voice `KIND_NAMES`
 *  already uses -- `generation/scenarios.py`'s own names (e.g. "Variance, not drift") were
 *  written for the engineers who built the gate, not for someone seeing this cold. Falls back
 *  to the stored NAME for any id this map has not caught up with. */
const TEST_NAMES: Record<string, string> = {
  F1: 'Transfer beats buying',
  F2: 'Two parts blocking the same assembly line',
  F3: "A part that's both short and holding up production",
  F4: "A supplier that's unpredictable, not just late",
  F5: "The cheapest quote isn't the cheapest supplier",
  F6: "The minimum order size isn't worth it",
  F7: "Demand shifted, the buffer didn't",
  F8: 'Stock that will never move',
  F9: 'A slow-moving part, correctly left alone',
  F10: 'Most of the warehouse, correctly left alone',
  F11: 'A smaller problem, correctly ranked first',
};

/** The reference bar. `erp_reorder_point` (minimum stock plus the delivery wait) rather than
 *  the cruder `erp_safety_stock` -- the harder comparison to beat is the more convincing one. */
const ERP_REFERENCE_ARM = 'erp_reorder_point';
const INCUMBENT_ARMS = ['erp_safety_stock', 'erp_reorder_point'];
const isIncumbent = (key: string) => INCUMBENT_ARMS.includes(key);

/** Indian units. Rs 57,762,429 is not a number anyone reads at a glance. */
function rupees(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e7) return `₹${(abs / 1e7).toFixed(1)} crore`;
  if (abs >= 1e5) return `₹${(abs / 1e5).toFixed(1)} lakh`;
  return `₹${Math.round(abs).toLocaleString('en-IN')}`;
}

export function SimulationPage() {
  const [engines, setEngines] = useState<string[]>(['automatic']);
  const [budget, setBudget] = useState<string>('');
  const [label, setLabel] = useState<string>('Proof run');
  const [starting, setStarting] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const toggle = (value: string) =>
    setEngines((prev) => (prev.includes(value) ? prev.filter((e) => e !== value) : [...prev, value]));

  async function start() {
    setStarting(true);
    setMessage(null);
    try {
      const res = await fetch('/api/simulations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engines, budget, label }),
      });
      const body = await res.json();
      if (!res.ok) {
        setMessage({ kind: 'error', text: body?.error ?? 'Could not start the test' });
        return;
      }
      setMessage({
        kind: 'ok',
        text: 'Test started. It takes about two minutes. Press "Show the latest results" when it is done.',
      });
    } catch {
      setMessage({ kind: 'error', text: 'Could not reach the server' });
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 px-4 py-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">The Benchmark</h1>
        <p className="text-muted-foreground max-w-prose">
          We hid known problems in a test warehouse, then let the system loose on it without telling it anything. This
          is what it found, next to what a traditional stock-threshold alert would find on the same warehouse.
        </p>
      </header>

      <SetupCard
        engines={engines}
        toggle={toggle}
        budget={budget}
        setBudget={setBudget}
        label={label}
        setLabel={setLabel}
        starting={starting}
        start={start}
        onRefresh={() => setReloadKey((k) => k + 1)}
        message={message}
      />

      <BenchmarkResults key={reloadKey} />
    </div>
  );
}

function SetupCard(props: {
  engines: string[];
  toggle: (v: string) => void;
  budget: string;
  setBudget: (v: string) => void;
  label: string;
  setLabel: (v: string) => void;
  starting: boolean;
  start: () => void;
  onRefresh: () => void;
  message: { kind: 'ok' | 'error'; text: string } | null;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Run the test</CardTitle>
        <CardDescription>
          Everything runs on the same practice warehouse, on the same day, so any difference is down to the method and
          not to luck. Your real stock is never touched.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label className="text-sm font-medium">Forecasting engine</Label>
          {ENGINE_CHOICES.map((choice) => (
            <div key={choice.value} className="flex items-start gap-2">
              <Checkbox
                id={`m-${choice.value}`}
                checked={props.engines.includes(choice.value)}
                onCheckedChange={() => props.toggle(choice.value)}
                className="mt-1"
              />
              <Label htmlFor={`m-${choice.value}`} className="font-normal cursor-pointer">
                {choice.label ?? choice.value}
              </Label>
            </div>
          ))}
          <p className="text-xs text-muted-foreground">
            The traditional stock-threshold alert you’re compared against always runs. It is the reference point, not an
            option.
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="sim-budget" className="text-sm font-medium">
              How many things may it tell you about at once?
            </Label>
            <Input
              id="sim-budget"
              inputMode="numeric"
              placeholder="leave blank to use your saved setting"
              value={props.budget}
              onChange={(e) => props.setBudget(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Anywhere from {BUDGET_SPEC.min} to {BUDGET_SPEC.max}. Doesn’t change this page -- every problem it can
              find is counted here, whether or not it made this run’s list.
            </p>
          </div>
          <div className="space-y-1">
            <Label htmlFor="sim-label" className="text-sm font-medium">
              Name this test
            </Label>
            <Input id="sim-label" value={props.label} onChange={(e) => props.setLabel(e.target.value)} />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={props.start} disabled={props.starting || props.engines.length === 0}>
            {props.starting ? 'Starting...' : 'Run the test'}
          </Button>
          <Button variant="outline" onClick={props.onRefresh}>
            Show the latest results
          </Button>
        </div>

        {props.message && (
          <Alert variant={props.message.kind === 'error' ? 'destructive' : 'default'}>
            <AlertDescription>{props.message.text}</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

/** Remounted by a changing `key` to refresh -- `useAnalyticsQuery` has no `refetch()`. */
function BenchmarkResults() {
  const q = useAnalyticsQuery('sim_type_summary', {});
  const rows = (q.data ?? []) as unknown as SimTypeSummary[];

  if (q.loading) return <Skeleton className="h-96 w-full" />;
  if (q.error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>
          We could not load past results. If no test has ever been run here, run one and then press “Show the latest
          results”.
        </AlertDescription>
      </Alert>
    );
  }
  if (rows.length === 0) {
    return (
      <Alert>
        <AlertDescription>No test has been run yet. Press “Run the test” above.</AlertDescription>
      </Alert>
    );
  }

  const latestBatch = rows[0].BATCH_ID;
  const latest = rows.filter((r) => r.BATCH_ID === latestBatch);
  const erp = latest.find((r) => r.CONSUMPTION_MODEL === ERP_REFERENCE_ARM);
  const oursEngine = latest.find((r) => !isIncumbent(r.CONSUMPTION_MODEL))?.CONSUMPTION_MODEL;
  const ours = latest
    .filter((r) => r.CONSUMPTION_MODEL === oursEngine)
    .slice()
    .sort((a, b) => b.COUNT - a.COUNT);

  if (!erp || ours.length === 0) {
    return (
      <Alert variant="destructive">
        <AlertDescription>This run is missing a side of the comparison. Run the test again.</AlertDescription>
      </Alert>
    );
  }

  return (
    <>
      <BenchmarkChart erp={erp} ours={ours} />
      <ElevenTests />
    </>
  );
}

function BenchmarkChart({ erp, ours }: { erp: SimTypeSummary; ours: SimTypeSummary[] }) {
  const [erpOpen, setErpOpen] = useState(true);
  const [openType, setOpenType] = useState<string | null>(ours[0]?.FINDING_TYPE ?? null);
  const max = Math.max(erp.COUNT, ...ours.map((r) => r.COUNT));

  return (
    <Card>
      <style>{`
        .bench { --erp: #c25e12; --ours: #0f6fb5; }
        @media (prefers-color-scheme: dark) {
          .bench { --erp: #c9782a; --ours: #3d92d1; }
        }
        .dark .bench { --erp: #c9782a; --ours: #3d92d1; }
      `}</style>
      <CardHeader>
        <CardTitle className="text-base">What each approach can even find</CardTitle>
        <CardDescription>
          Counts of real issues found on a test warehouse where the answers were fixed in advance, before the system saw
          any of it.
        </CardDescription>
      </CardHeader>
      <CardContent className="bench space-y-6">
        <div>
          <button
            type="button"
            onClick={() => setErpOpen((v) => !v)}
            aria-expanded={erpOpen}
            className="w-full rounded-md text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <div>
                <span className="text-sm font-medium">Traditional stock-threshold alert</span>
                <span className="ml-2 text-xs text-muted-foreground">— what a typical ERP does today</span>
              </div>
              <span className="shrink-0 text-xs text-muted-foreground">
                {erpOpen ? 'hide the rule' : 'what does 20 mean?'}
              </span>
            </div>
            <BenchmarkBar value={erp.COUNT} max={max} colorVar="--erp" />
          </button>
          {erpOpen && (
            <div className="mb-1 mt-2 space-y-1.5 rounded-md bg-muted/60 p-3">
              <p className="text-sm">
                <strong>The rule:</strong> flag a part once stock on hand drops to or below the safety stock plus what
                it expects to use while waiting for the next delivery. It never checks whether that expected usage is
                still accurate — only whether the line has been crossed.
              </p>
              {erp.EXAMPLE_SUBJECT && <p className="font-mono text-xs text-muted-foreground">{erp.EXAMPLE_SUBJECT}</p>}
              <p className="text-sm">{erp.EXAMPLE_REASONING}</p>
            </div>
          )}
        </div>

        <div className="space-y-3 border-t pt-5">
          <div className="text-sm font-medium">This system, by kind of problem</div>
          <div className="space-y-1">
            {ours.map((row) => (
              <TypeRow
                key={row.FINDING_TYPE}
                row={row}
                max={max}
                open={openType === row.FINDING_TYPE}
                onToggle={() => setOpenType((cur) => (cur === row.FINDING_TYPE ? null : row.FINDING_TYPE))}
              />
            ))}
          </div>
        </div>

        <p className="max-w-prose border-t pt-4 text-sm text-muted-foreground">
          A traditional stock-threshold alert can only ever produce that first bar — it has one test, “buy more.” This
          system runs eight different tests over the same stock and suppliers, on the same day.
        </p>
      </CardContent>
    </Card>
  );
}

function BenchmarkBar({ value, max, colorVar }: { value: number; max: number; colorVar: '--erp' | '--ours' }) {
  const pct = max > 0 ? Math.max((value / max) * 100, 3) : 0;
  return (
    <div className="flex items-center gap-3">
      <div className="h-3 flex-1 overflow-hidden rounded-sm bg-muted">
        <div
          className="h-full rounded-sm transition-[width] duration-300"
          style={{ width: `${pct}%`, background: `var(${colorVar})` }}
        />
      </div>
      <span className="w-8 shrink-0 text-right text-sm font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function TypeRow({
  row,
  max,
  open,
  onToggle,
}: {
  row: SimTypeSummary;
  max: number;
  open: boolean;
  onToggle: () => void;
}) {
  const label = KIND_NAMES[row.FINDING_TYPE] ?? row.FINDING_TYPE;
  return (
    <div className="rounded-md">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full rounded-md py-1.5 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-sm">{label}</span>
          <span className="text-xs text-muted-foreground">
            {open ? 'hide how it found this' : 'how did it find this?'}
          </span>
        </div>
        <BenchmarkBar value={row.COUNT} max={max} colorVar="--ours" />
      </button>
      {open && (
        <div className="mb-2 mt-2 space-y-1.5 rounded-md bg-muted/60 p-3">
          {row.EXAMPLE_SUBJECT && <p className="font-mono text-xs text-muted-foreground">{row.EXAMPLE_SUBJECT}</p>}
          <p className="text-sm">{row.EXAMPLE_REASONING}</p>
          {row.EXAMPLE_EXPOSURE > 0 && (
            <p className="text-xs text-muted-foreground">
              About {rupees(row.EXAMPLE_EXPOSURE)} at stake in this example — simulated on the test warehouse, not
              measured savings.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------- the eleven planted tests ---------------- */

/** Numeric sort on "F1".."F11" -- string order would put F10/F11 before F2. */
function testNumber(findingId: string): number {
  return Number.parseInt(findingId.replace(/^F/, ''), 10) || 0;
}

function ElevenTests() {
  const q = useAnalyticsQuery('sim_benchmark', {});
  const rows = (q.data ?? []) as unknown as SimBenchmarkCheck[];

  if (q.loading) return <Skeleton className="h-64 w-full" />;
  if (q.error || rows.length === 0) {
    // Not every deploy has run the notebook far enough to populate this table yet -- the
    // benchmark chart above still stands on its own, so this section just stays out of the
    // way rather than showing an alarming error for a table that may simply be new.
    return null;
  }

  const latestBatch = rows[0].BATCH_ID;
  const checks = rows
    .filter((r) => r.BATCH_ID === latestBatch)
    .slice()
    .sort((a, b) => testNumber(a.FINDING_ID) - testNumber(b.FINDING_ID));

  const passed = checks.filter((c) => c.RESULT === 'PASS').length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Eleven tests, checked by hand</CardTitle>
        <CardDescription>
          Before any of this existed, we wrote down eleven specific problems and hid them in the test warehouse —
          including two where the right answer is to say nothing at all. Every row names the real part or supplier, so
          you can check it yourself.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="mb-4 flex items-baseline gap-2">
          <span className="text-3xl font-semibold tabular-nums">
            {passed} of {checks.length}
          </span>
          <span className="text-sm text-muted-foreground">passed</span>
        </div>
        <ul className="divide-y">
          {checks.map((check) => (
            <TestRow key={check.FINDING_ID} check={check} />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function TestRow({ check }: { check: SimBenchmarkCheck }) {
  const label = TEST_NAMES[check.FINDING_ID] ?? check.NAME;
  const passed = check.RESULT === 'PASS';
  const resultText = passed ? 'Passed' : check.RESULT === 'CHECK' ? 'Needs a look' : 'Failed';

  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-sm font-medium">{label}</span>
        <span className="flex shrink-0 items-center gap-2">
          <span
            className={
              passed
                ? 'text-sm font-semibold'
                : check.RESULT === 'CHECK'
                  ? 'text-sm font-semibold text-amber-600 dark:text-amber-500'
                  : 'text-sm font-semibold text-destructive'
            }
          >
            {passed ? '✓ ' : check.RESULT === 'CHECK' ? '⚠ ' : '✗ '}
            {resultText}
          </span>
        </span>
      </div>
      {check.SUBJECT && <p className="font-mono text-xs text-muted-foreground">{check.SUBJECT}</p>}
      <p className="mt-1 text-sm text-muted-foreground">{check.DETAIL}</p>
    </li>
  );
}
