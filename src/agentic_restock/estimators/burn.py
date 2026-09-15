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
"""

from __future__ import annotations

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

# Client-forced methods. These bypass the selection above entirely: the point of offering
# them is that a client can say "just do what my ERP does" and compare, so second-guessing
# the choice per part would defeat it. What they cannot bypass is reporting a spread --
# every method must return one, because the risk model reads sigma_d, not the rate.
METHOD_SIMPLE_AVERAGE = "SIMPLE_AVERAGE"
METHOD_RECENT_ONLY = "RECENT_ONLY"

MODEL_AUTOMATIC = "automatic"
MODEL_SIMPLE_AVERAGE = "simple_average"
MODEL_RECENT_ONLY = "recent_only"

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
        return self.method == METHOD_CROSTON


def estimate_burn(
    issues: np.ndarray,
    *,
    end_date: date,
    horizon_days: int,
    model: str = MODEL_AUTOMATIC,
    recent_days: int = 90,
) -> BurnEstimate:
    """Estimate the burn rate from a daily issue series, oldest first.

    `end_date` is the date of the LAST element — needed to line the seasonal buckets up with the
    calendar. `horizon_days` is the replenishment lead time, i.e. how far ahead the forward burn
    should look.

    `model` is the client's choice. `automatic` is the branch ladder described in the module
    docstring and is what ships; the other two force one method across every part, which a
    client asks for in order to compare against their ERP or to discard history after a step
    change in the business. An unrecognised value falls back to automatic rather than raising --
    a settings row that predates a rename must not stop a scan.
    """
    series = np.asarray(issues, dtype=float)
    n = len(series)
    if n == 0:
        return BurnEstimate(0.0, 0.0, 0.0, CONFIDENCE_LOW, METHOD_TRAILING_MEAN, 1.0, 0)

    zero_fraction = float((series == 0).mean())

    if model == MODEL_SIMPLE_AVERAGE:
        return _flat_mean(series, zero_fraction, METHOD_SIMPLE_AVERAGE)
    if model == MODEL_RECENT_ONLY:
        window = series[-max(int(recent_days), 1) :]
        return _flat_mean(window, zero_fraction, METHOD_RECENT_ONLY, observations=n)

    if zero_fraction > INTERMITTENCY_THRESHOLD:
        return _croston(series, zero_fraction)

    if n < MIN_DAYS_FOR_SEASONALITY:
        # No seasonal claim is possible, and saying so matters: the risk model widens sigma_DL on
        # LOW confidence, which correctly ranks poorly-understood parts with less conviction
        # instead of assigning them false certainty.
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

    return _level_times_season(series, end_date=end_date, horizon_days=horizon_days,
                              zero_fraction=zero_fraction)


def _flat_mean(
    series: np.ndarray,
    zero_fraction: float,
    method: str,
    observations: int | None = None,
) -> BurnEstimate:
    """A plain mean over whatever window the client asked for, with its own spread.

    No seasonality, no intermittency handling, no staleness branch -- that is the whole
    point of the forced models. Two consequences the panel has to disclose rather than
    correct for: a seasonal part is stocked to its annual average in both its busy and its
    quiet season, and under `recent_only` a part that moves less often than the window
    reports zero burn, which reads downstream as stock that is no longer moving.
    """
    n = len(series)
    level = float(series.mean()) if n else 0.0
    sigma = float(series.std(ddof=1)) if n > 1 else 0.0
    # Confidence tracks how well the rate is known, not whether the method suits the part.
    # A forced method on a thin window is a weak estimate however good the method is.
    confidence = CONFIDENCE_MEDIUM if n >= MIN_DAYS_FOR_SEASONALITY else CONFIDENCE_LOW
    if level > 0 and sigma / level > 0.6:
        confidence = CONFIDENCE_LOW
    return BurnEstimate(
        level=level,
        forward_burn=level,
        sigma_d=sigma,
        confidence=confidence,
        method=method,
        zero_day_fraction=zero_fraction,
        observations=observations if observations is not None else n,
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

    # Has it stopped? Croston averages over the whole window, so a part whose demand ended
    # months ago still reports the rate it used to run at -- and a dead-capital detector looking
    # for "no longer moving" would see a healthy burn and 40 days of cover instead of stock that
    # will never be consumed. Silence for several multiples of the part's OWN demand interval is
    # the signal; a fixed number of days would call a slow mover dead.
    days_since_last_issue = n - 1 - int(nonzero_days[-1])
    if days_since_last_issue > max(STALE_INTERVAL_MULTIPLE * mean_interval, MIN_STALE_DAYS):
        return BurnEstimate(
            level=0.0,
            forward_burn=0.0,
            sigma_d=0.0,
            confidence=CONFIDENCE_MEDIUM,
            method=METHOD_STOPPED,
            zero_day_fraction=zero_fraction,
            observations=n,
        )

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
