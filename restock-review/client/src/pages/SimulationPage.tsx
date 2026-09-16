import { useMemo, useState } from 'react';
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
  Separator,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@databricks/appkit-ui/react';
import { SETTINGS_BY_KEY } from '../../../shared/settingsSpec';

/**
 * Written for a client reading this page cold, not for us.
 *
 * Every number on screen has to survive "what does that mean?" from someone who has never heard
 * of recall, precision, exposure or an output budget. So this page carries no ratios, no net
 * value and no confusion matrix. It says four things anyone can check: how many parts were
 * really about to run out, how many the system spotted, how many it put in front of you, and how
 * often it was wrong. The comparison between the two methods falls straight out of the second.
 *
 * The analysis-grade figures -- net value, budget cost, recall, precision -- are still written to
 * `sim_run` on every run. They are simply not what this page is for.
 */

type SimRun = {
  RUN_ID: string;
  BATCH_ID: string;
  RUN_TS: string;
  LABEL: string;
  CONSUMPTION_MODEL: string;
  BUDGET: number;
  PAIRS_TOTAL: number;
  PAIRS_AT_RISK: number;
  CAUGHT: number;
  MISSED: number;
  FALSE_ALARMS: number;
  DETECTOR_CAUGHT: number;
  DETECTOR_FALSE_ALARMS: number;
  VALUE_DELIVERED: number;
  VALUE_MISSED: number;
};

const ENGINE_CHOICES = SETTINGS_BY_KEY['consumption_model'].choices ?? [];
const BUDGET_SPEC = SETTINGS_BY_KEY['items_per_notification'];

/** Named by the algorithm, exactly as Settings names the same two choices. One name for one
 *  thing: a client who reads both pages should not have to work out that "our own method" and
 *  "Croston + seasonal decomposition" are the same option. */
const METHOD_NAMES: Record<string, string> = {
  automatic: 'Croston + seasonal decomposition',
  statsforecast: 'Croston SBA + AutoETS (Nixtla StatsForecast)',
};

function methodName(key: string): string {
  return METHOD_NAMES[key] ?? key;
}

/** Indian units. Rs 57,762,429 is not a number anyone reads at a glance. */
function rupees(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e7) return `₹${(abs / 1e7).toFixed(1)} crore`;
  if (abs >= 1e5) return `₹${(abs / 1e5).toFixed(1)} lakh`;
  return `₹${Math.round(abs).toLocaleString('en-IN')}`;
}

export function SimulationPage() {
  const [engines, setEngines] = useState<string[]>(ENGINE_CHOICES.map((c) => c.value));
  const [budget, setBudget] = useState<string>('');
  const [label, setLabel] = useState<string>('Method comparison');
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
        text: 'Test started. It takes about a minute and a half. Press "Show latest results" when it is done.',
      });
    } catch {
      setMessage({ kind: 'error', text: 'Could not reach the server' });
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 px-4 py-6">
      <style>{`
        .sim { --m0:#2a78d6; --m1:#eb6834; --m2:#1baf7a; --m3:#eda100; --track:#e6e5e1; }
        @media (prefers-color-scheme: dark) {
          .sim { --m0:#3987e5; --m1:#d95926; --m2:#199e70; --m3:#c98500; --track:#2e2e2b; }
        }
        .dark .sim { --m0:#3987e5; --m1:#d95926; --m2:#199e70; --m3:#c98500; --track:#2e2e2b; }
        .sim-track { background:var(--track); border-radius:6px; height:12px; overflow:hidden; }
        .sim-fill { height:12px; border-radius:0 6px 6px 0; min-width:3px; }
      `}</style>

      <div className="sim space-y-6">
        <header className="space-y-1">
          <h1 className="text-2xl font-semibold">Compare forecasting methods</h1>
          <p className="text-muted-foreground max-w-prose">
            See how each method performs on a test warehouse where we already know which parts run
            out, so you can check its answers. Test data, not your real stock.
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

        <ResultsSection key={reloadKey} />
      </div>
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
        <CardTitle className="text-base">Run a test</CardTitle>
        <CardDescription>
          Both methods are tried on exactly the same practice warehouse, so any difference is down
          to the method and not to luck.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label className="text-sm font-medium">Which methods should we try?</Label>
          {ENGINE_CHOICES.map((choice) => {
            const name = methodName(choice.value);
            return (
              <div key={choice.value} className="flex items-start gap-2">
                <Checkbox
                  id={`m-${choice.value}`}
                  checked={props.engines.includes(choice.value)}
                  onCheckedChange={() => props.toggle(choice.value)}
                  className="mt-1"
                />
                <Label htmlFor={`m-${choice.value}`} className="font-normal cursor-pointer">
                  {name}
                </Label>
              </div>
            );
          })}
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
              Anywhere from {BUDGET_SPEC.min} to {BUDGET_SPEC.max}. Try a higher number to see how
              much more it would catch, and how much longer your list gets.
            </p>
          </div>
          <div className="space-y-1">
            <Label htmlFor="sim-label" className="text-sm font-medium">
              Name this test
            </Label>
            <Input
              id="sim-label"
              value={props.label}
              onChange={(e) => props.setLabel(e.target.value)}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={props.start} disabled={props.starting || props.engines.length === 0}>
            {props.starting ? 'Starting...' : 'Run the test'}
          </Button>
          <Button variant="outline" onClick={props.onRefresh}>
            Show latest results
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
function ResultsSection() {
  const { data, loading, error } = useAnalyticsQuery('sim_runs', {});
  const runs = (data ?? []) as unknown as SimRun[];

  const latest = useMemo(() => {
    if (runs.length === 0) return [];
    const batch = runs[0].BATCH_ID;
    return runs.filter((r) => r.BATCH_ID === batch);
  }, [runs]);

  if (loading) return <Skeleton className="h-72 w-full" />;
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>
          We could not load past results. If no test has ever been run here, run one and then press
          "Show latest results".
        </AlertDescription>
      </Alert>
    );
  }
  if (runs.length === 0) {
    return (
      <Alert>
        <AlertDescription>No test has been run yet. Press "Run the test" above.</AlertDescription>
      </Alert>
    );
  }

  return (
    <>
      {latest.length > 0 && <Results runs={latest} />}
      <HowToRead />
      <History runs={runs} />
    </>
  );
}

function Results({ runs }: { runs: SimRun[] }) {
  const first = runs[0];
  const best = runs.reduce((a, b) => (b.DETECTOR_CAUGHT > a.DETECTOR_CAUGHT ? b : a));
  const worst = runs.reduce((a, b) => (b.DETECTOR_CAUGHT < a.DETECTOR_CAUGHT ? b : a));
  const gap = best.DETECTOR_CAUGHT - worst.DETECTOR_CAUGHT;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">The test warehouse</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 sm:grid-cols-3">
            <Fact big={first.PAIRS_TOTAL} label="parts we checked" />
            <Fact
              big={first.PAIRS_AT_RISK}
              label="were really about to run out"
              note="We know this because we built the warehouse. It is the answer sheet."
            />
            <Fact
              big={first.BUDGET}
              label="was all it could report"
              note="On purpose. A list of four gets acted on; a list of forty gets ignored."
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">How each method did</CardTitle>
          <CardDescription>
            Out of the {first.PAIRS_AT_RISK} parts that were genuinely heading for a shortage.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {runs.map((run, i) => (
            <MethodResult key={run.RUN_ID} run={run} index={i} />
          ))}

          {runs.length > 1 && (
            <>
              <Separator />
              {gap !== 0 ? (
                <p className="text-sm">
                  <strong>{methodName(best.CONSUMPTION_MODEL)}</strong> spotted{' '}
                  <strong>
                    {gap} more {gap === 1 ? 'shortage' : 'shortages'}
                  </strong>{' '}
                  than {methodName(worst.CONSUMPTION_MODEL)} on this test:{' '}
                  {best.DETECTOR_CAUGHT} against {worst.DETECTOR_CAUGHT}, out of{' '}
                  {first.PAIRS_AT_RISK} that were really coming.
                </p>
              ) : (
                <p className="text-sm">
                  Both methods spotted the same number of shortages on this test. Try a different
                  number of items per list to see them separate.
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Fact({ big, label, note }: { big: number | string; label: string; note?: string }) {
  return (
    <div>
      <p className="text-3xl font-semibold tabular-nums">{big}</p>
      <p className="text-sm">{label}</p>
      {note && <p className="mt-1 text-xs text-muted-foreground">{note}</p>}
    </div>
  );
}

function MethodResult({ run, index }: { run: SimRun; index: number }) {
  const name = methodName(run.CONSUMPTION_MODEL);
  const share = run.PAIRS_AT_RISK > 0 ? run.DETECTOR_CAUGHT / run.PAIRS_AT_RISK : 0;
  const colour = `var(--m${index % 4})`;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="inline-block h-3 w-3 rounded-sm" style={{ background: colour }} />
          <span className="font-medium">{name}</span>
        </div>
        <span className="text-sm">
          <strong className="tabular-nums">{run.DETECTOR_CAUGHT}</strong> of {run.PAIRS_AT_RISK}{' '}
          shortages spotted
        </span>
      </div>

      <div className="sim-track">
        <div
          className="sim-fill"
          style={{ width: `${Math.max(share * 100, 1)}%`, background: colour }}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-3 text-sm">
        <Line
          value={`${run.CAUGHT} of them`}
          label="put in front of you in this one run"
          note="The rest come back at the next check. Nothing is thrown away."
        />
        <Line
          value={`${run.FALSE_ALARMS}`}
          label={run.FALSE_ALARMS === 1 ? 'false alarm' : 'false alarms'}
          note="Times it asked you to act on a part that was actually fine."
        />
        <Line
          value={rupees(run.VALUE_DELIVERED)}
          label="worth of trouble flagged (test data)"
          note="What the problems it showed you would have cost if left alone."
        />
      </div>
    </div>
  );
}

function Line({ value, label, note }: { value: string; label: string; note: string }) {
  return (
    <div>
      <p className="font-semibold tabular-nums">{value}</p>
      <p className="text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-xs text-muted-foreground">{note}</p>
    </div>
  );
}

function HowToRead() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">How to read this</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm text-muted-foreground max-w-prose">
        <p>
          <strong className="text-foreground">How do we know the right answers?</strong> We built
          the warehouse ourselves. We decided in advance how fast every part would be used and how
          much stock it had, so we know exactly which ones were going to run out. The system is told
          none of that. It only sees the same kind of history it would see in your business.
        </p>
        <p>
          <strong className="text-foreground">
            Why does it report only a few when it spotted more?
          </strong>{' '}
          Because a short list gets acted on and a long one gets ignored. The system checks twice a
          day and each time puts forward only the most valuable few. Anything it spotted but had no
          room for comes back at the next check.
        </p>
        <p>
          <strong className="text-foreground">What is a false alarm?</strong> A part it asked you to
          do something about that turned out to be fine. A few are unavoidable. A lot would mean you
          stop trusting it, and that is the real cost.
        </p>
        <p>
          <strong className="text-foreground">What are the two methods?</strong> Two ways of working
          out how fast a part is being used: one ours, one a well-known open-source library. Each
          handles steady parts and stop-start parts differently. You can pick either in Settings,
          and this page is how you decide which.
        </p>
      </CardContent>
    </Card>
  );
}

function History({ runs }: { runs: SimRun[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Earlier tests</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Test</TableHead>
                <TableHead>Method</TableHead>
                <TableHead className="text-right">Spotted</TableHead>
                <TableHead className="text-right">Reported</TableHead>
                <TableHead className="text-right">False alarms</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.RUN_ID}>
                  <TableCell>
                    <div className="font-medium">{run.LABEL}</div>
                    <div className="text-xs text-muted-foreground">{run.RUN_TS}</div>
                  </TableCell>
                  <TableCell>{methodName(run.CONSUMPTION_MODEL)}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {run.DETECTOR_CAUGHT} of {run.PAIRS_AT_RISK}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{run.CAUGHT}</TableCell>
                  <TableCell className="text-right tabular-nums">{run.FALSE_ALARMS}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
