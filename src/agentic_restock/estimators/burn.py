"""E1 — forward burn rate, from a daily ISSUE series.

The first of the two estimators every other number inherits
(docs/intelligence_layer_design.md §2). Pure functions over numpy arrays: no Spark, no
Databricks, so the Phase 2 gate can run as a local test against the same series the generator
produces.

Three branches, and the order is the point:

1. **Intermittency is tested first.** Above ~70% zero days a daily mean is not merely imprecise,
   it is meaningless — the series is lumps separated by gaps, and averaging them describes
   neither. Most MRO and spare demand looks like this. Routed to Croston.
2. **Otherwise level x season**, with the level *deseasonalised* so it is comparable across the
   calendar and the forward window re-seasonalised for the horizon that matters.
3. **Too little history** short-circuits both: under 90 days there is no seasonal signal to
   find, so it returns a trailing mean and says so.

What it returns is deliberately two numbers, not one. `level` is the daily rate currently in
force; `forward_burn` is what will be consumed over the replenishment horizon, which differs
whenever the part is seasonal. Collapsing them would make the estimate untestable — you could
not tell a wrong level from a right level in a busy season.

That ladder is the `automatic` **engine**. A second engine (`statsforecast`) answers the same
three branches with Nixtla's estimators — CrostonSBA for the lumpy branch, AutoETS on monthly
totals for the seasonal one — and is selected per client from the admin panel. It is a second
implementation of the same questions, not a different routing, which is what lets the simulation
feature attribute a difference in outcome to the estimator rather than to the ladder
(docs/simulation_feature_design.md).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

# Above this fraction of zero days, a daily mean stops describing the series.
INTERMITTENCY_THRESHOLD = 0.70

# Below this much history there is no seasonal signal worth extracting.
MIN_DAYS_FOR_SEASONALITY = 90

# The recent window the level is read from. Long enough to average out daily noise, short enough
# to sit inside one seasonal phase and to have moved after a step change.
LEVEL_WINDOW_DAYS = 56

# Seasonal indices are estimated on calendar months: 12 buckets over 3 years is ~3 observations
# each, which is thin but usable. Finer buckets would be noise.
MONTHS = 12

# Full seasonal weight needs this many years. Below it the index is shrunk toward 1.
YEARS_FOR_FULL_SEASONAL_WEIGHT = 3.0

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

METHOD_CROSTON = "CROSTON"
METHOD_SEASONAL = "LEVEL_X_SEASON"
METHOD_TRAILING_MEAN = "TRAILING_MEAN"
METHOD_STOPPED = "STOPPED"

# Nixtla's estimators, behind the same contract. A second *engine* rather than a fourth
# method: it answers the same two questions (is this part lumpy, and what will it consume
# over the replenishment horizon) with a different implementation of each branch, so the
# simulation can compare engines on the same drawn world instead of comparing a method
# against a ladder. See docs/simulation_feature_design.md §3.
METHOD_SF_CROSTON_SBA = "SF_CROSTON_SBA"
METHOD_SF_AUTO_ETS = "SF_AUTO_ETS"

MODEL_AUTOMATIC = "automatic"
MODEL_STATSFORECAST = "statsforecast"


# AutoETS needs two full cycles before a seasonal claim is worth making; below this it is
# fitted without a seasonal component rather than fitted badly with one.
MIN_MONTHS_FOR_SEASONAL_ETS = 24

# A lumpy part counts as stopped once it has been silent for this many multiples of its own
# demand interval. Expressed as a multiple rather than a fixed number of days because a part that
# normally issues every 90 days is not dead after 100 -- but one that issues weekly is.
STALE_INTERVAL_MULTIPLE = 4.0
MIN_STALE_DAYS = 90.0


@dataclass(frozen=True)
class BurnEstimate:
    """What E1 recovers. `level` and `forward_burn` differ only for seasonal parts."""

    level: float
    """Daily rate currently in force, deseasonalised. Comparable across the calendar."""

    forward_burn: float
    """Expected daily rate over the replenishment horizon — level re-seasonalised."""

    sigma_d: float
    """Daily demand standard deviation. Feeds sigma_DL in the risk model."""

    confidence: str
    method: str
    zero_day_fraction: float
    observations: int
    seasonal_index_now: float = 1.0
    seasonal_index_ahead: float = 1.0

    @property
    def is_intermittent(self) -> bool:
        # Both engines' intermittent branches, or a caller reading this would conclude that
        # switching engine had made a lumpy part smooth.
        return self.method in (METHOD_CROSTON, METHOD_SF_CROSTON_SBA)


def estimate_burn(
    issues: np.ndarray,
    *,
    end_date: date,
    horizon_days: int,
    model: str = MODEL_AUTOMATIC,
) -> BurnEstimate:
    """Estimate the burn rate from a daily issue series, oldest first.

    `end_date` is the date of the LAST element — needed to line the seasonal buckets up with the
    calendar. `horizon_days` is the replenishment lead time, i.e. how far ahead the forward burn
    should look.

    `model` selects the engine: `automatic` is the branch ladder described in the module
    docstring and is what ships, `statsforecast` answers the same branches with Nixtla's
    estimators. An unrecognised value falls back to automatic rather than raising -- a settings
    row that predates a rename must not stop a scan.
    """
    series = np.asarray(issues, dtype=float)
    n = len(series)
    if n == 0:
        return BurnEstimate(0.0, 0.0, 0.0, CONFIDENCE_LOW, METHOD_TRAILING_MEAN, 1.0, 0)

    zero_fraction = float((series == 0).mean())

    if model == MODEL_STATSFORECAST:
        return _statsforecast(
            series, end_date=end_date, horizon_days=horizon_days, zero_fraction=zero_fraction
        )

    if zero_fraction > INTERMITTENCY_THRESHOLD:
        return _croston(series, zero_fraction)

    if n < MIN_DAYS_FOR_SEASONALITY:
        return _short_history(series, zero_fraction)

    return _level_times_season(series, end_date=end_date, horizon_days=horizon_days,
                              zero_fraction=zero_fraction)


def _short_history(series: np.ndarray, zero_fraction: float) -> BurnEstimate:
    """Under 90 days: a trailing mean, reported as LOW.

    No seasonal claim is possible, and saying so matters: the risk model widens sigma_DL on
    LOW confidence, which correctly ranks poorly-understood parts with less conviction
    instead of assigning them false certainty.

    Shared by every engine deliberately. Missing history is a property of the data, not of
    the estimator, and an engine that reported a confident number here would be claiming to
    recover a signal that is not in the series.
    """
    n = len(series)
    window = series[-min(n, LEVEL_WINDOW_DAYS) :]
    level = float(window.mean())
    return BurnEstimate(
        level=level,
        forward_burn=level,
        sigma_d=float(window.std(ddof=1)) if len(window) > 1 else 0.0,
        confidence=CONFIDENCE_LOW,
        method=METHOD_TRAILING_MEAN,
        zero_day_fraction=zero_fraction,
        observations=n,
    )


def _demand_has_stopped(series: np.ndarray) -> bool:
    """Has this part gone quiet for long enough to call it stopped?

    Any rate estimator that averages over a window reports the rate a part *used to* run at
    long after its demand ended -- and a dead-capital detector looking for "no longer moving"
    would then see a healthy burn and 40 days of cover instead of stock that will never be
    consumed. Silence for several multiples of the part's OWN demand interval is the signal; a
    fixed number of days would call a slow mover dead.

    Shared by every engine, because this is a business rule about what the number means
    downstream, not a forecasting method. No package returns "this part is dead".
    """
    nonzero_days = np.flatnonzero(series > 0)
    if len(nonzero_days) < 2:
        return False
    mean_interval = float(np.diff(nonzero_days).mean())
    days_since_last_issue = len(series) - 1 - int(nonzero_days[-1])
    return days_since_last_issue > max(STALE_INTERVAL_MULTIPLE * mean_interval, MIN_STALE_DAYS)


def _stopped(zero_fraction: float, observations: int) -> BurnEstimate:
    return BurnEstimate(
        level=0.0,
        forward_burn=0.0,
        sigma_d=0.0,
        confidence=CONFIDENCE_MEDIUM,
        method=METHOD_STOPPED,
        zero_day_fraction=zero_fraction,
        observations=observations,
    )


def _croston(series: np.ndarray, zero_fraction: float) -> BurnEstimate:
    """Croston's method: model issue SIZE and the INTERVAL between issues separately.

    The rate is `mean_size / mean_interval`. Reported sigma is the raw daily standard deviation
    rather than a decomposed one — for lumpy demand the day-to-day spread is what the risk model
    actually needs, and an analytic Croston variance would be a more precise answer to a
    different question.
    """
    nonzero_days = np.flatnonzero(series > 0)
    n = len(series)

    if len(nonzero_days) < 2:
        # One issue, or none, in the whole window. There is no interval to measure.
        level = float(series.mean())
        return BurnEstimate(
            level=level,
            forward_burn=level,
            sigma_d=float(series.std(ddof=1)) if n > 1 else 0.0,
            confidence=CONFIDENCE_LOW,
            method=METHOD_CROSTON,
            zero_day_fraction=zero_fraction,
            observations=n,
        )

    sizes = series[nonzero_days]
    intervals = np.diff(nonzero_days)
    mean_interval = float(intervals.mean())
    rate = float(sizes.mean()) / mean_interval if mean_interval > 0 else float(sizes.mean())

    if _demand_has_stopped(series):
        return _stopped(zero_fraction, n)

    # Enough events to trust the interval? Ten is a rough floor for a stable mean.
    confidence = CONFIDENCE_MEDIUM if len(nonzero_days) >= 10 else CONFIDENCE_LOW

    return BurnEstimate(
        level=rate,
        forward_burn=rate,
        sigma_d=float(series.std(ddof=1)),
        confidence=confidence,
        method=METHOD_CROSTON,
        zero_day_fraction=zero_fraction,
        observations=n,
    )


def _month_of(end_date: date, n: int) -> np.ndarray:
    """Calendar month (1-12) for every element of a series ending at `end_date`."""
    start = end_date - timedelta(days=n - 1)
    return np.array([(start + timedelta(days=i)).month for i in range(n)])


def seasonal_indices(series: np.ndarray, months: np.ndarray, years: float) -> np.ndarray:
    """Multiplicative index per calendar month, shrunk toward 1 when history is short.

    Shrinkage is `min(years / 3, 1)`: with one year there is a single observation per month and
    the "index" is largely noise, so it is pulled most of the way back to 1. At three years it
    passes through unchanged. Without this, short-history pairs get confidently wrong seasonal
    corrections — and because the term is exactly 1.0 at 36 months, the dataset deliberately
    includes shorter cohorts so this path is exercised at all.
    """
    overall = series.mean()
    index = np.ones(MONTHS + 1)
    if overall <= 0:
        return index

    weight = min(years / YEARS_FOR_FULL_SEASONAL_WEIGHT, 1.0)
    for month in range(1, MONTHS + 1):
        mask = months == month
        if mask.sum() >= 7:  # a week of observations before claiming a monthly effect
            raw = series[mask].mean() / overall
            index[month] = 1.0 + (raw - 1.0) * weight
    return index


def _level_times_season(
    series: np.ndarray, *, end_date: date, horizon_days: int, zero_fraction: float
) -> BurnEstimate:
    n = len(series)
    months = _month_of(end_date, n)
    years = n / 365.0
    index = seasonal_indices(series, months, years)

    # Deseasonalise before reading the level, or a part measured in its busy season looks
    # permanently busier than it is.
    daily_index = index[months]
    deseasonalised = np.divide(
        series, daily_index, out=np.zeros_like(series), where=daily_index > 0
    )

    window = deseasonalised[-LEVEL_WINDOW_DAYS:]
    level = float(window.mean())

    index_now = float(daily_index[-LEVEL_WINDOW_DAYS:].mean())
    ahead_months = np.array(
        [(end_date + timedelta(days=d)).month for d in range(1, max(horizon_days, 1) + 1)]
    )
    index_ahead = float(index[ahead_months].mean())

    residual = window - level
    sigma_d = float(residual.std(ddof=1)) if len(window) > 1 else 0.0

    if years >= 2.0:
        confidence = CONFIDENCE_HIGH
    elif years >= 1.0:
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW

    # A very noisy series should not be reported as well understood however long it is.
    if level > 0 and sigma_d / level > 0.6:
        confidence = CONFIDENCE_LOW if confidence == CONFIDENCE_MEDIUM else CONFIDENCE_MEDIUM

    return BurnEstimate(
        level=level,
        forward_burn=level * index_ahead,
        sigma_d=sigma_d,
        confidence=confidence,
        method=METHOD_SEASONAL,
        zero_day_fraction=zero_fraction,
        observations=n,
        seasonal_index_now=index_now,
        seasonal_index_ahead=index_ahead,
    )


# --- the statsforecast engine ------------------------------------------------------------
#
# A second engine behind the same contract, not a fourth method. It answers the *same two
# questions* the automatic ladder answers -- is this part lumpy, and what will it consume over
# the replenishment horizon -- with Nixtla's estimator for each branch instead of ours. Keeping
# the ladder identical is what makes the comparison in the simulation feature meaningful: one
# variable changes, not the routing as well.
#
# What it does NOT own, and why: `confidence`, the STOPPED rule, and the level/forward_burn
# split are consumed downstream (`risk.py` widens sigma_DL on LOW confidence, S4 DEAD_CAPITAL
# reads STOPPED). No forecasting package returns any of them, so they stay here and both engines
# produce the same eight-field struct.


def _load_statsforecast():
    """Import lazily, and fail loudly rather than falling back.

    An unrecognised `model` value falls back to automatic (see `estimate_burn`) because a
    settings row that predates a rename must not stop a scan. A *recognised* engine whose
    package is missing is the opposite case: it is a deployment fault, and silently running
    `automatic` while the admin panel shows `statsforecast` is precisely the "your system
    ignored me" failure that tests/test_settings_spec_parity.py exists to prevent. A job that
    fails with a package name is diagnosable; a number quietly produced by the wrong engine is
    not.
    """
    try:
        from statsforecast import models
    except ImportError as exc:  # pragma: no cover - exercised only where the package is absent
        raise ImportError(
            "consumption_model='statsforecast' requires the statsforecast package, which is not "
            "installed in this environment. Install it, or set the consumption model back to "
            "'automatic' in the admin panel."
        ) from exc
    return models


def _statsforecast(
    series: np.ndarray, *, end_date: date, horizon_days: int, zero_fraction: float
) -> BurnEstimate:
    """Nixtla's estimators, routed by the same ladder as `automatic`."""
    models = _load_statsforecast()
    n = len(series)

    if zero_fraction > INTERMITTENCY_THRESHOLD:
        if _demand_has_stopped(series):
            return _stopped(zero_fraction, n)
        return _sf_croston(series, zero_fraction, models)

    if n < MIN_DAYS_FOR_SEASONALITY:
        return _short_history(series, zero_fraction)

    return _sf_auto_ets(
        series,
        end_date=end_date,
        horizon_days=horizon_days,
        zero_fraction=zero_fraction,
        models=models,
    )


def _sf_croston(series: np.ndarray, zero_fraction: float, models) -> BurnEstimate:
    """CrostonSBA -- the Syntetos-Boylan bias correction on Croston's method.

    Classic Croston's rate is positively biased: it is a ratio of two smoothed means, and the
    expectation of the ratio is not the ratio of the expectations. SBA applies the correction
    term. That is the substantive difference from `_croston` above, which takes unweighted means
    over the whole window and so also weights a three-year-old issue like last week's.

    The forecast is flat -- Croston-family models emit a constant rate -- so `level` and
    `forward_burn` are the same number, exactly as in `_croston`.
    """
    n = len(series)
    events = int((series > 0).sum())

    if events < 2:
        # One issue, or none. There is no interval to smooth, and SBA has nothing to correct.
        level = float(series.mean()) if n else 0.0
        return BurnEstimate(
            level=level,
            forward_burn=level,
            sigma_d=float(series.std(ddof=1)) if n > 1 else 0.0,
            confidence=CONFIDENCE_LOW,
            method=METHOD_SF_CROSTON_SBA,
            zero_day_fraction=zero_fraction,
            observations=n,
        )

    rate = float(np.asarray(models.CrostonSBA().forecast(y=series, h=1)["mean"])[0])
    rate = max(rate, 0.0)

    return BurnEstimate(
        level=rate,
        forward_burn=rate,
        # The raw daily spread, as in `_croston`: for lumpy demand it is what the risk model
        # needs, and holding it identical across engines keeps the comparison to one variable.
        sigma_d=float(series.std(ddof=1)),
        confidence=CONFIDENCE_MEDIUM if events >= 10 else CONFIDENCE_LOW,
        method=METHOD_SF_CROSTON_SBA,
        zero_day_fraction=zero_fraction,
        observations=n,
    )


def _complete_months(
    series: np.ndarray, end_date: date
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Monthly totals for whole calendar months only, oldest first.

    Partial months at either end are dropped rather than scaled up. A half-observed month
    carried into a seasonal fit reads as a collapse in demand, which is the one thing a
    seasonal model must not be told.
    """
    n = len(series)
    start = end_date - timedelta(days=n - 1)

    totals: dict[tuple[int, int], float] = {}
    counts: dict[tuple[int, int], int] = {}
    for offset in range(n):
        day = start + timedelta(days=offset)
        key = (day.year, day.month)
        totals[key] = totals.get(key, 0.0) + float(series[offset])
        counts[key] = counts.get(key, 0) + 1

    keys = sorted(totals)
    whole = [k for k in keys if counts[k] == calendar.monthrange(k[0], k[1])[1]]
    return np.array([totals[k] for k in whole], dtype=float), whole


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _sf_auto_ets(
    series: np.ndarray,
    *,
    end_date: date,
    horizon_days: int,
    zero_fraction: float,
    models,
) -> BurnEstimate:
    """AutoETS on monthly totals -- the engine's answer to `_level_times_season`.

    Monthly rather than daily, deliberately. The seasonality that matters here is annual (the
    festive ramp, see the generator spec), and no daily-frequency ETS can carry a 365-period
    cycle. Aggregating to months gives the model exactly the information `_level_times_season`
    uses -- 36 monthly observations, 3 cycles -- so the two are compared on equal footing rather
    than one being handed a finer series than the other.

    `level` is the trailing twelve-month daily rate. A full year averages the seasonal cycle out
    by construction, which is the same argument `generation/ground_truth.py` makes for grading
    against a deseasonalised rate.
    """
    n = len(series)
    totals, months = _complete_months(series, end_date)

    if len(totals) < 3:
        # Enough days to have passed the seasonality gate, but not enough whole months to fit
        # anything monthly -- a February-to-April window, say. Report it as what it is.
        return _short_history(series, zero_fraction)

    season_length = 12 if len(totals) >= MIN_MONTHS_FOR_SEASONAL_ETS else 1
    last_year, last_month = months[-1]

    horizon_days = max(int(horizon_days), 1)
    target = end_date + timedelta(days=horizon_days)
    months_ahead = max((target.year - last_year) * 12 + (target.month - last_month), 1)

    forecast = np.asarray(
        models.AutoETS(season_length=season_length).forecast(y=totals, h=months_ahead)["mean"],
        dtype=float,
    )
    # ETS is additive in levels and will happily project a declining series below zero. Demand
    # cannot be negative, and a negative forward burn would read downstream as infinite cover.
    forecast = np.clip(forecast, 0.0, None)

    rate_by_day: dict[date, float] = {}
    for step in range(months_ahead):
        year, month = _add_months(last_year, last_month, step + 1)
        days_in_month = calendar.monthrange(year, month)[1]
        daily_rate = float(forecast[step]) / days_in_month
        for day_of_month in range(1, days_in_month + 1):
            rate_by_day[date(year, month, day_of_month)] = daily_rate

    # Slice the forecast to the replenishment window itself. The last complete month usually
    # ends before `end_date`, so the horizon starts partway into the first forecast month --
    # taking whole months instead would shift every seasonal part by up to four weeks.
    horizon_rates = [
        rate_by_day[end_date + timedelta(days=step)]
        for step in range(1, horizon_days + 1)
        if end_date + timedelta(days=step) in rate_by_day
    ]

    trailing = min(len(totals), 12)
    trailing_days = sum(calendar.monthrange(y, m)[1] for y, m in months[-trailing:])
    level = float(totals[-trailing:].sum() / trailing_days) if trailing_days else 0.0
    forward_burn = float(np.mean(horizon_rates)) if horizon_rates else level

    window = series[-LEVEL_WINDOW_DAYS:]
    # Empirical, not model-derived. A conformal or analytic interval would change the spread
    # and the level at once, and the simulation could not attribute the difference to either.
    sigma_d = float(window.std(ddof=1)) if len(window) > 1 else 0.0

    years = n / 365.0
    if years >= 2.0:
        confidence = CONFIDENCE_HIGH
    elif years >= 1.0:
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW
    if level > 0 and sigma_d / level > 0.6:
        confidence = CONFIDENCE_LOW if confidence == CONFIDENCE_MEDIUM else CONFIDENCE_MEDIUM

    return BurnEstimate(
        level=level,
        forward_burn=forward_burn,
        sigma_d=sigma_d,
        confidence=confidence,
        method=METHOD_SF_AUTO_ETS,
        zero_day_fraction=zero_fraction,
        observations=n,
        # Whatever the fitted model implies, rather than a separately estimated index: the two
        # must agree or `forward_burn` and the index shown beside it tell different stories.
        seasonal_index_now=float(window.mean() / level) if level > 0 else 1.0,
        seasonal_index_ahead=float(forward_burn / level) if level > 0 else 1.0,
    )


# ---------------------------------------------------------------------------
# Observed demand shift -- has the rate actually changed, within the history?
# ---------------------------------------------------------------------------

SHIFT_RECENT_WINDOW_DAYS = 90
SHIFT_GAP_DAYS = 90
SHIFT_PRIOR_WINDOW_DAYS = 90
SHIFT_MIN_HISTORY_DAYS = (
    SHIFT_RECENT_WINDOW_DAYS + SHIFT_GAP_DAYS + SHIFT_PRIOR_WINDOW_DAYS
)


@dataclass(frozen=True)
class DemandShift:
    """How the consumption rate now compares with the rate before.

    This exists because S6 was asking the wrong question. It compared the corrected forward burn
    against the snapshot's recorded flat average, which is a test of whether our estimator
    *disagrees with theirs* — not a test of whether demand changed. Both failure modes were
    measured on the benchmark dataset:

    - **It missed a real step.** A 1.6x step planted 120 days ago showed a ratio of only 1.16,
      because the recorded average is itself computed over a window that already absorbs the
      post-step days. Both numbers had moved, so the gap between them understated the change.
    - **It fired on lumpy demand.** An intermittent spare with 92% zero days showed a ratio of
      1.78 purely because Croston estimates an intermittent rate differently from a flat daily
      mean. Nothing had changed; the two numbers describe the same series.

    So the test is now a genuine before/after within the pair's own history, and the two windows
    are separated by a gap: with adjacent windows a step part-way through the prior window
    contaminates its own baseline and dilutes the ratio it is supposed to reveal.
    """

    recent_rate: float
    prior_rate: float
    ratio: float
    observable: bool
    """False when there is too little history to compare, or nothing moved in the prior window.
    A ratio against a zero baseline is not a large shift, it is no baseline — reporting one is
    how a part that simply started being used gets sold as a demand surge."""


def observed_shift(series: np.ndarray) -> DemandShift:
    """Mean daily rate over the last 90 days against the 90 days ending 180 days ago."""
    n = len(series)
    if n < SHIFT_MIN_HISTORY_DAYS:
        return DemandShift(0.0, 0.0, 1.0, False)

    recent = float(np.mean(series[-SHIFT_RECENT_WINDOW_DAYS:]))
    prior_end = n - SHIFT_RECENT_WINDOW_DAYS - SHIFT_GAP_DAYS
    prior = float(np.mean(series[prior_end - SHIFT_PRIOR_WINDOW_DAYS : prior_end]))
    if prior <= 0:
        return DemandShift(recent, prior, 1.0, False)
    return DemandShift(recent, prior, recent / prior, True)
