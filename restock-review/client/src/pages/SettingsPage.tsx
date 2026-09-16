import { useMemo, useState } from 'react';
import { useAnalyticsQuery } from '@databricks/appkit-ui/react';
import {
  Alert,
  AlertDescription,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
  RadioGroup,
  RadioGroupItem,
  Separator,
  Skeleton,
} from '@databricks/appkit-ui/react';
import { SETTINGS, SETTINGS_BY_KEY, type SettingSpec } from '../../../shared/settingsSpec';

type SettingValue = number | string;
type Values = Record<string, SettingValue>;

const SECTIONS: Array<{ title: string; blurb: string; keys: string[] }> = [
  {
    title: 'Working out what you will need',
    blurb:
      'Before we can say a part is about to run short, we have to work out how fast you are using it. Most people leave this alone.',
    keys: ['consumption_model'],
  },
  {
    title: 'What it costs you to hold stock',
    blurb: 'This is what turns "a lot of stock sitting there" into a rupee figure you can act on.',
    keys: ['holding_rate'],
  },
  {
    title: 'How cautious we should be',
    blurb: 'How careful we are when suggesting you move stock around, and when we call stock idle.',
    keys: ['transfer_caution', 'dead_stock_cover_days', 'dead_stock_min_value'],
  },
  {
    title: 'What we actually send you',
    blurb: 'This decides what is worth interrupting you for. It does not change any of the numbers above.',
    keys: ['items_per_notification', 'min_exposure'],
  },
];

function defaults(): Values {
  return Object.fromEntries(SETTINGS.map((s) => [s.key, s.default]));
}

function formatNumber(value: SettingValue): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString('en-IN');
}

export function SettingsPage() {
  // Remounting the loader is how a fresh read happens after a save --
  // useAnalyticsQuery has no refetch(). Same pattern as FulfillingOrdersPage.
  const [refreshKey, setRefreshKey] = useState(0);
  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-foreground">Settings</h2>
        <p className="text-sm text-muted-foreground">
          These decide what we recommend, and when we interrupt you. Changes take effect the next time we check
          &mdash; anything you have already approved stays exactly as it is.
        </p>
      </div>
      <SettingsForm key={refreshKey} onSaved={() => setRefreshKey((k) => k + 1)} />
    </div>
  );
}

function SettingsForm({ onSaved }: { onSaved: () => void }) {
  const { data, loading, error } = useAnalyticsQuery('app_settings', {});
  const [edited, setEdited] = useState<Values>({});
  const [saveState, setSaveState] = useState<{ status: 'idle' | 'saving' | 'error' | 'saved'; message?: string }>({
    status: 'idle',
  });

  // Stored rows over defaults. A key nobody has ever written is absent rather than null,
  // so an untouched table and a table of explicit defaults behave identically -- which is
  // the same rule the pipeline follows when it resolves settings.
  const stored = useMemo(() => {
    const values = defaults();
    for (const row of data ?? []) {
      if (!SETTINGS_BY_KEY[row.setting_key]) continue;
      try {
        values[row.setting_key] = JSON.parse(row.setting_value) as SettingValue;
      } catch {
        // Leave the default. A malformed row costs one setting, not the page.
      }
    }
    return values;
  }, [data]);

  const lastChange = useMemo(() => {
    const rows = data ?? [];
    if (rows.length === 0) return null;
    const newest = rows.reduce((a, b) => (a.updated_at > b.updated_at ? a : b));
    return { by: newest.updated_by || 'unknown', at: String(newest.updated_at) };
  }, [data]);

  const values: Values = { ...stored, ...edited };
  const dirtyKeys = Object.keys(edited).filter((k) => edited[k] !== stored[k]);
  const isDirty = dirtyKeys.length > 0;

  function set(key: string, value: SettingValue) {
    setEdited((e) => ({ ...e, [key]: value }));
    setSaveState({ status: 'idle' });
  }

  async function save() {
    setSaveState({ status: 'saving' });
    try {
      const payload = Object.fromEntries(dirtyKeys.map((k) => [k, values[k]]));
      const res = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: payload }),
      });
      const body = (await res.json().catch(() => ({}))) as { error?: string };
      if (!res.ok) throw new Error(body.error ?? `Request failed (${res.status})`);
      setEdited({});
      setSaveState({ status: 'saved' });
      onSaved();
    } catch (err) {
      setSaveState({ status: 'error', message: err instanceof Error ? err.message : 'Failed to save' });
    }
  }

  if (loading) {
    return (
      <Card>
        <CardContent className="space-y-3 pt-6">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-2/3" />
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>
          Could not load settings. The pipeline is still running on its defaults, so nothing is broken &mdash; but
          changes cannot be made until this loads. ({String(error)})
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-6">
      {SECTIONS.map((section) => (
        <Card key={section.title}>
          <CardHeader>
            <CardTitle>{section.title}</CardTitle>
            <CardDescription>{section.blurb}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            {section.keys.map((key, i) => (
              <div key={key} className="space-y-4">
                {i > 0 && <Separator />}
                <SettingControl spec={SETTINGS_BY_KEY[key]} values={values} onChange={set} />
              </div>
            ))}
          </CardContent>
        </Card>
      ))}

      {saveState.status === 'error' && (
        <Alert variant="destructive">
          <AlertDescription>{saveState.message}</AlertDescription>
        </Alert>
      )}
      {saveState.status === 'saved' && (
        <Alert>
          <AlertDescription>Saved. These apply from the next scan.</AlertDescription>
        </Alert>
      )}

      <div className="flex items-center gap-3">
        <Button onClick={() => void save()} disabled={!isDirty || saveState.status === 'saving'}>
          {saveState.status === 'saving' ? 'Saving…' : `Save changes${isDirty ? ` (${dirtyKeys.length})` : ''}`}
        </Button>
        <Button variant="outline" onClick={() => setEdited(defaults())} disabled={saveState.status === 'saving'}>
          Restore recommended
        </Button>
        {lastChange && (
          <span className="text-xs text-muted-foreground ml-auto">
            Last changed by {lastChange.by} · {lastChange.at.slice(0, 10)}
          </span>
        )}
      </div>
    </div>
  );
}

function SettingControl({
  spec,
  values,
  onChange,
}: {
  spec: SettingSpec;
  values: Values;
  onChange: (key: string, value: SettingValue) => void;
}) {
  const value = values[spec.key];

  return (
    <div className="space-y-2">
      <Label className="text-sm font-medium">{spec.label}</Label>
      {spec.help && <p className="text-sm text-muted-foreground">{spec.help}</p>}

      {spec.kind === 'choice' ? (
        <RadioGroup
          value={String(value)}
          onValueChange={(v: string) => onChange(spec.key, v)}
          className="space-y-2 pt-1"
        >
          {spec.choices?.map((choice) => {
            const selected = String(value) === choice.value;
            return (
              <div key={choice.value} className="space-y-1">
                <div className="flex items-start gap-2">
                  <RadioGroupItem value={choice.value} id={`${spec.key}-${choice.value}`} className="mt-1" />
                  <div className="space-y-1">
                    <Label htmlFor={`${spec.key}-${choice.value}`} className="font-normal cursor-pointer">
                      {choice.label}
                    </Label>
                    {selected && choice.help && (
                      <p className="text-xs text-muted-foreground max-w-prose">{choice.help}</p>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </RadioGroup>
      ) : (
        <NumberField spec={spec} value={value} onChange={onChange} />
      )}

      {spec.affects && spec.affects.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-1">
          <span className="text-xs text-muted-foreground mr-1">Changes:</span>
          {spec.affects.map((a) => (
            <Badge key={a} variant="outline" className="text-xs font-normal">
              {a}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

function NumberField({
  spec,
  value,
  onChange,
}: {
  spec: SettingSpec;
  value: SettingValue;
  onChange: (key: string, value: SettingValue) => void;
}) {
  // Round to the declared precision. 0.14 * 100 is 14.000000000000002 in IEEE-754, and
  // a client reading that on a settings page reasonably concludes the product is unfinished.
  const raw = spec.asPercent ? Number(value) * 100 : Number(value);
  const shown = Number.isFinite(raw) ? Number(raw.toFixed(spec.decimals ?? 2)) : raw;
  const min = spec.asPercent && spec.min !== undefined ? spec.min * 100 : spec.min;
  const max = spec.asPercent && spec.max !== undefined ? spec.max * 100 : spec.max;
  const outOfRange = (min !== undefined && shown < min) || (max !== undefined && shown > max);
  const warn = spec.warnAbove !== undefined && Number(value) > spec.warnAbove;

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        {spec.unit === '₹' && <span className="text-sm text-muted-foreground">₹</span>}
        <Input
          type="number"
          className="w-40"
          value={Number.isFinite(shown) ? shown : ''}
          min={min}
          max={max}
          step={spec.kind === 'int' ? 1 : 'any'}
          onChange={(e) => {
            const raw = Number(e.target.value);
            onChange(spec.key, spec.asPercent ? raw / 100 : raw);
          }}
        />
        {spec.unit && spec.unit !== '₹' && <span className="text-sm text-muted-foreground">{spec.unit}</span>}
      </div>
      {outOfRange && (
        <p className="text-xs text-destructive">
          Must be between {formatNumber(min ?? 0)} and {max !== undefined ? formatNumber(max) : 'any'}.
        </p>
      )}
      {warn && (
        <p className="text-xs text-amber-600 dark:text-amber-500">
          A long list gets ignored &mdash; which is the problem this product exists to fix.
        </p>
      )}
    </div>
  );
}
