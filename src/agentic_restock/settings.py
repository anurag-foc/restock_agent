"""Client-configurable settings — the admin panel's data layer.

Everything the detectors do is driven by two kinds of number: the four we *forecast*
(burn rate, burn spread, lead time, lead-time spread) and the dozen or so we *assume*
(what stock costs to hold, how many items a PM can act on, when stock counts as dead).
The assumed ones were hardcoded. They are not ours to choose — they are the client's
inventory policy — so this module moves them into a table an admin panel can write.

Three rules hold the design together:

**Defaults equal the previously hardcoded values, exactly.** An empty settings table
must reproduce the behaviour that shipped before it existed. That makes the migration a
no-op until somebody deliberately changes something, and it means a failed read degrades
to "correct" rather than to "zero".

**Resolved settings are passed down, never read globally.** The detectors and estimators
are pure functions over pandas/numpy precisely so the accuracy gates can run as local
tests without a cluster. A module-level `read_settings()` call inside a scanner would
undo that. The job reads the table once and hands a `Settings` object down.

**Validation lives here, not in the UI.** A browser form is a convenience, not a
boundary — the same bounds have to hold when a setting arrives from a notebook, a
migration or a direct SQL write. `coerce()` is the only way values enter.

The table is append-only and the latest row per (key, scope) wins. That is deliberate:
it gives "who changed what, when" without a second audit table, and it means a bad
change can be reverted by writing the old value rather than by deleting anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from agentic_restock.config import qualified_table

TABLE_APP_SETTINGS = "app_settings"

# Only GLOBAL is exposed today. The column exists so per-plant and per-category scopes --
# the natural second ask once a client has lived with the panel -- do not need a migration.
SCOPE_GLOBAL = "GLOBAL"


class SettingError(ValueError):
    """A setting value that is out of range, the wrong type, or not a known key."""


@dataclass(frozen=True)
class Spec:
    """One configurable setting: what it is, what it defaults to, and what it may be.

    `default` is the value that was hardcoded before this module existed. Changing a
    default changes behaviour for every client who has not overridden it, so treat it as
    a product decision rather than a tidy-up.
    """

    key: str
    kind: str  # "float" | "int" | "choice"
    default: Any
    label: str
    """Plain-language name, as it appears in the admin panel."""

    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    warn_above: float | None = None
    """Accepted, but the panel should say something. Above this a value is legal and
    probably a mistake -- 9 items per notification is not an error, it is the alert
    fatigue this product exists to remove."""


# --- the registry ----------------------------------------------------------
#
# Seven controls, ten keys: three controls carry a parameter of their own, which only
# appears in the panel when its parent option is selected.

SPECS: tuple[Spec, ...] = (
    # --- forecasting -------------------------------------------------------
    Spec(
        key="consumption_model",
        kind="choice",
        default="automatic",
        label="Consumption model",
        choices=("automatic", "simple_average", "recent_only"),
    ),
    Spec(
        key="consumption_recent_days",
        kind="int",
        default=90,
        label="Recent history window",
        minimum=30,
        maximum=365,
    ),
    Spec(
        key="leadtime_model",
        kind="choice",
        default="recent_weighted",
        label="Supplier lead time model",
        choices=("recent_weighted", "ignore_outliers"),
    ),
    Spec(
        key="leadtime_recency",
        kind="choice",
        default="normal",
        label="How quickly old deliveries fade",
        choices=("fast", "normal", "slow"),
    ),
    # --- what stock costs --------------------------------------------------
    Spec(
        key="holding_rate",
        kind="float",
        default=0.14,
        label="Cost of holding stock",
        minimum=0.05,
        maximum=0.40,
    ),
    # --- recommendations ---------------------------------------------------
    Spec(
        key="transfer_caution",
        kind="choice",
        default="balanced",
        label="Care taken with the donating warehouse",
        choices=("relaxed", "balanced", "protective"),
    ),
    Spec(
        key="dead_stock_cover_days",
        kind="float",
        default=180.0,
        label="Days of cover before stock counts as not moving",
        minimum=30.0,
        maximum=730.0,
    ),
    Spec(
        key="dead_stock_min_value",
        kind="float",
        default=50_000.0,
        label="Minimum value before dead stock is raised",
        minimum=1_000.0,
    ),
    # --- what reaches you --------------------------------------------------
    Spec(
        key="items_per_notification",
        kind="int",
        default=4,
        label="Items per notification",
        minimum=1,
        maximum=10,
        warn_above=6,
    ),
    Spec(
        key="min_exposure",
        kind="float",
        default=25_000.0,
        label="Minimum value to raise anything",
        minimum=1_000.0,
    ),
)

SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}


# --- preset expansions -----------------------------------------------------
#
# A preset is one choice in the panel standing in for two numbers in the code. The point
# is not to hide the numbers but to avoid asking a client to reason about two dials that
# only make sense together: "protect 1.5x safety stock" and "leave 45 days of cover" are
# the same intent expressed twice, and a client who sets one without the other gets an
# incoherent policy rather than a stricter one.

TRANSFER_CAUTION_PRESETS: dict[str, tuple[float, float]] = {
    # name: (donor protection multiple, minimum days of cover left at the donor)
    "relaxed": (0.5, 7.0),
    "balanced": (1.0, 21.0),  # the previously hardcoded pair
    "protective": (1.5, 45.0),
}

LEADTIME_RECENCY_HALF_LIFE_DAYS: dict[str, float] = {
    "fast": 90.0,
    "normal": 180.0,  # the previously hardcoded value
    "slow": 365.0,
}


# --- coercion and validation ----------------------------------------------


def coerce(key: str, value: Any) -> Any:
    """Parse and bounds-check one setting. The only supported way in.

    Raises `SettingError` rather than clamping. A silently clamped value is a setting a
    client believes they changed and did not -- which surfaces later as "the system
    ignored me", the hardest class of bug to be told about.
    """
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key!r}")

    if spec.kind == "choice":
        text = str(value)
        if text not in spec.choices:
            raise SettingError(
                f"{key}: {text!r} is not one of {', '.join(spec.choices)}"
            )
        return text

    try:
        parsed = int(value) if spec.kind == "int" else float(value)
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{key}: {value!r} is not a {spec.kind}") from exc

    if spec.minimum is not None and parsed < spec.minimum:
        raise SettingError(f"{key}: {parsed} is below the minimum of {spec.minimum}")
    if spec.maximum is not None and parsed > spec.maximum:
        raise SettingError(f"{key}: {parsed} is above the maximum of {spec.maximum}")
    return parsed


def warnings_for(key: str, value: Any) -> list[str]:
    """Advisory messages for a legal but questionable value. Never blocks a write."""
    spec = SPEC_BY_KEY.get(key)
    if spec is None or spec.warn_above is None:
        return []
    if float(value) > spec.warn_above:
        return [
            (
                f"{spec.label}: {value} is high. A long list gets ignored, "
                f"which is the problem this product exists to fix."
            )
        ]
    return []


# --- the resolved object ---------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Every setting in force for one run, defaults filled in.

    Built once at job start and passed down. The derived properties exist so callers ask
    for the number they actually need (`donor_protection_multiple`) rather than knowing
    that it comes from a preset -- which keeps the preset an admin-panel concept instead
    of leaking into the detectors.
    """

    values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        if key not in SPEC_BY_KEY:
            raise SettingError(f"unknown setting {key!r}")
        return self.values.get(key, SPEC_BY_KEY[key].default)

    # --- forecasting ---
    @property
    def consumption_model(self) -> str:
        return self["consumption_model"]

    @property
    def consumption_recent_days(self) -> int:
        return int(self["consumption_recent_days"])

    @property
    def leadtime_model(self) -> str:
        return self["leadtime_model"]

    @property
    def leadtime_half_life_days(self) -> float:
        return LEADTIME_RECENCY_HALF_LIFE_DAYS[self["leadtime_recency"]]

    # --- money ---
    @property
    def holding_rate(self) -> float:
        return float(self["holding_rate"])

    # --- transfers ---
    @property
    def donor_protection_multiple(self) -> float:
        return TRANSFER_CAUTION_PRESETS[self["transfer_caution"]][0]

    @property
    def donor_min_cover_after_days(self) -> float:
        return TRANSFER_CAUTION_PRESETS[self["transfer_caution"]][1]

    # --- thresholds ---
    @property
    def dead_stock_cover_days(self) -> float:
        return float(self["dead_stock_cover_days"])

    @property
    def dead_stock_min_value(self) -> float:
        return float(self["dead_stock_min_value"])

    @property
    def items_per_notification(self) -> int:
        return int(self["items_per_notification"])

    @property
    def min_exposure(self) -> float:
        return float(self["min_exposure"])

    def as_dict(self) -> dict[str, Any]:
        """Every setting including defaulted ones — what gets stamped onto a run.

        Stamping the *resolved* set rather than the overridden rows is the point: a run
        has to be reproducible from its own record, and "the defaults at the time"
        is exactly the thing that will have moved by the time anyone asks.
        """
        return {spec.key: self[spec.key] for spec in SPECS}


DEFAULTS = Settings(values={})


def resolve(rows: list[dict] | None) -> Settings:
    """Latest row per key wins; anything absent or unparseable falls back to the default.

    A bad row is skipped rather than fatal. The alternative is a malformed value in one
    setting stopping the whole scan, which trades a slightly-wrong number for no
    recommendations at all -- a much worse failure for something that runs unattended.
    """
    if not rows:
        return DEFAULTS

    values: dict[str, Any] = {}
    for row in rows:
        key = row.get("SETTING_KEY") or row.get("setting_key")
        raw = row.get("SETTING_VALUE") or row.get("setting_value")
        if key not in SPEC_BY_KEY:
            continue
        try:
            values[key] = coerce(key, json.loads(raw) if isinstance(raw, str) else raw)
        except (SettingError, json.JSONDecodeError):
            continue  # keep the default; see docstring
    return Settings(values=values)


# --- SQL -------------------------------------------------------------------


def build_settings_table_ddl(catalog: str | None = None, schema: str | None = None) -> str:
    """Append-only: the newest row per (key, scope) is the value in force.

    No UPDATE path, so change history is the table itself and a revert is a write rather
    than a delete.
    """
    table = qualified_table(TABLE_APP_SETTINGS, catalog, schema)
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
      setting_key STRING COMMENT 'Key from settings.SPECS',
      scope_type STRING COMMENT 'GLOBAL today; PLANT / CATEGORY reserved',
      scope_id STRING COMMENT 'NULL when scope_type = GLOBAL',
      setting_value STRING COMMENT 'JSON-encoded so the type survives the round trip',
      updated_at TIMESTAMP COMMENT 'When this value was written',
      updated_by STRING COMMENT 'Who changed it, for the panel history line'
    )
    COMMENT 'Client-configurable settings. Append-only; latest row per (key, scope) wins.'
    """.strip()


def build_settings_read_query(catalog: str | None = None, schema: str | None = None) -> str:
    """The values in force right now — one row per key, newest write."""
    table = qualified_table(TABLE_APP_SETTINGS, catalog, schema)
    return f"""
    SELECT setting_key AS SETTING_KEY, setting_value AS SETTING_VALUE
    FROM (
      SELECT
        setting_key,
        setting_value,
        ROW_NUMBER() OVER (
          PARTITION BY setting_key, scope_type, COALESCE(scope_id, '')
          ORDER BY updated_at DESC
        ) AS rn
      FROM {table}
      WHERE scope_type = '{SCOPE_GLOBAL}'
    )
    WHERE rn = 1
    """.strip()


def build_settings_write(
    key: str,
    value: Any,
    *,
    updated_by: str,
    catalog: str | None = None,
    schema: str | None = None,
) -> str:
    """One INSERT for one setting. Value is validated before the SQL is built.

    Building the statement only after `coerce` succeeds means an out-of-range value can
    never reach the table, whatever calls this -- the panel, a notebook, or a migration.
    """
    checked = coerce(key, value)
    table = qualified_table(TABLE_APP_SETTINGS, catalog, schema)
    payload = json.dumps(checked).replace("'", "''")
    who = str(updated_by).replace("'", "''")
    return f"""
    INSERT INTO {table}
      (setting_key, scope_type, scope_id, setting_value, updated_at, updated_by)
    VALUES
      ('{key}', '{SCOPE_GLOBAL}', NULL, '{payload}', current_timestamp(), '{who}')
    """.strip()


def build_settings_history_query(
    limit: int = 50, catalog: str | None = None, schema: str | None = None
) -> str:
    """Every change, newest first — the panel's "last changed by" line and its audit view."""
    table = qualified_table(TABLE_APP_SETTINGS, catalog, schema)
    return f"""
    SELECT setting_key, setting_value, updated_at, updated_by
    FROM {table}
    ORDER BY updated_at DESC
    LIMIT {int(limit)}
    """.strip()


def _json_default(obj: Any) -> str:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def snapshot_json(settings: Settings) -> str:
    """The resolved settings as JSON, for stamping onto a scan run."""
    return json.dumps(settings.as_dict(), sort_keys=True, default=_json_default)
