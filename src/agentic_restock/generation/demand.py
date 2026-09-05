"""Demand processes — per-pair parameters and the daily issue series.

This is the module the burn estimator (E1) will be measured against. Each (part, warehouse) pair
gets a *generative model* with known true parameters, and the daily ISSUE series is drawn from
it. Phase 2's gate asks whether E1 recovers those parameters from the series alone.

Six regimes, assigned deterministically to hit the shares in docs/dataset_generator_spec.md
§3.1. Two of them exist specifically to exercise estimator branches that would otherwise be dead
code:

- **intermittent** (~15%): >70% zero days, so a daily mean is not merely imprecise but
  meaningless. Routes E1 to Croston. Most real MRO/spare demand looks like this.
- **erratic** (~12%): CV ~0.9 with no structure. Should come out LOW confidence and be ranked
  with less conviction, not acted on with false precision.

Determinism comes from each pair's own identity, never a global RNG stream: `numpy.random`
seeded once and consumed in loop order would make every pair's series shift whenever a pair was
added or the working set reordered. Here, adding a pair perturbs nothing else.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from functools import cache

import numpy as np

from agentic_restock.generation import contracts, dates, policy
from agentic_restock.generation.scenarios import (
    FINDINGS,
    HISTORY_DEPTH_COHORTS,
    SHRUNK_HISTORY_PAIRS,
    SPARSE_HISTORY_PAIRS,
    expect_finding,
    pair_universe,
)

FULL_HISTORY_DAYS = HISTORY_DEPTH_COHORTS["full"]["days"]

# Regime shares as a 100-slot cycle (spec §3.1: 30/25/10/8/15/12), then permuted once with a
# fixed seed. The permutation matters: with the regimes in blocks, a working set that is not a
# multiple of 100 over-represents whichever regimes sit early in the list and under-represents
# the tail. At 278 pairs that skewed smooth to 36% and erratic to 8% against a 12% target.
def _build_regime_cycle() -> list[str]:
    cycle = (
        ["smooth"] * 30
        + ["seasonal"] * 25
        + ["trending"] * 10
        + ["step"] * 8
        + ["intermittent"] * 15
        + ["erratic"] * 12
    )
    np.random.default_rng(_REGIME_PERMUTATION_SEED).shuffle(cycle)
    return cycle


_REGIME_PERMUTATION_SEED = 20260904

# --- Consumption volume ----------------------------------------------------
#
# Burn is driven by PRODUCTION VOLUME, not by unit cost. An earlier version had it inverse to
# cost -- cheap parts fast, expensive assemblies slow -- which is backwards for vehicle
# manufacturing: a plant building ~400 vehicles a day consumes ~400 engine assemblies a day. The
# inversion put expensive parts under 1 unit/day, where integer rounding turns a smooth series
# into lumpy noise and the recorded regime no longer describes the data.
#
# Plant stores feed the line, so they move production volumes. RDCs serve aftermarket/spares
# demand, which is genuinely a small fraction of production and genuinely lumpy for slow movers.

PLANT_VEHICLES_PER_DAY = 400
RDC_SPARES_FRACTION = 0.04

# Non-intermittent regimes need enough volume that integer rounding is immaterial.
MIN_SMOOTH_LEVEL = 5.0

_NOISE_CV = {
    "smooth": 0.15,
    "seasonal": 0.20,
    "trending": 0.20,
    "step": 0.18,
    "intermittent": 0.30,
    "erratic": 0.90,
}

# Indian manufacturing seasonality: a festive-season ramp peaking late October and a
# fiscal-year-end push in March. Modelled as two cosines rather than a flat sinusoid so the
# shape is not trivially recoverable by fitting a single harmonic.
_FESTIVE_PEAK_DOY = 300
_FISCAL_PEAK_DOY = 80

DEFAULT_LEAD_DAYS = 30

# Safety stock has to be lead-time aware. A flat number of days cannot work: at 18 days against
# a 40-day lead time the policy is structurally under-buffered, every reorder cycle troughs below
# the buffer, and 57% of pairs end up short -- which is the correct outcome of a broken policy,
# not a dataset worth detecting on. Buffer covers lead-time *variability* (z x sigma_lead), with a
# floor so a perfectly consistent supplier still carries something.
MIN_SAFETY_DAYS = 8


@dataclass(frozen=True)
class PairParams:
    """The true generative parameters for one (part, warehouse) pair."""

    part_id: str
    warehouse_id: str
    regime: str
    level: float
    noise_cv: float
    history_days: int
    history_cohort: str
    seasonal_amplitude: float = 0.0
    trend_per_day: float = 0.0
    step_day: int | None = None
    step_factor: float = 1.0
    mean_interval_days: float = 0.0
    mean_issue_size: float = 0.0
    quiet_since_day: int | None = None
    safety_stock: int = 0
    max_stock: int = 0
    unit_cost: float = 0.0
    criticality_class: str = "B"
    finding_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_level(self) -> float:
        """Daily burn as of today — after any step change. What E1 should recover."""
        return self.level * self.step_factor if self.step_day is not None else self.level


def _seed(*parts: object) -> int:
    """Stable integer seed from an identity. `hash()` is salted per process and unusable here."""
    return zlib.crc32("|".join(str(p) for p in parts).encode()) & 0xFFFFFFFF


def _rng(*parts: object) -> np.random.Generator:
    return np.random.default_rng(_seed(*parts))


def _base_level(warehouse_id: str, bom_level: int, index: int) -> float:
    """Daily consumption, driven by production volume rather than unit cost.

    Plant stores consume at line rate: vehicles/day x units per vehicle, scaled by how many of
    the 12 models actually use the part. RDCs carry aftermarket demand at a few percent of that,
    which is where genuinely slow-moving, lumpy spares live.
    """
    units_per_vehicle = 1 + (index % 3)  # 1-3, stands in for BOM multiplicities
    model_share = 0.25 + 0.06 * (index % 8)  # a quarter to three quarters of the model mix

    if warehouse_id in ("WH001", "WH002"):
        # Split across two plants, and deeper BOM levels aggregate across more parents.
        depth_factor = {0: 1.0, 1: 1.3, 2: 1.6}[bom_level]
        level = PLANT_VEHICLES_PER_DAY * 0.5 * units_per_vehicle * model_share * depth_factor
    else:
        level = (
            PLANT_VEHICLES_PER_DAY
            * RDC_SPARES_FRACTION
            * units_per_vehicle
            * model_share
        )

    return float(round(level, 1))


@cache
def _lead_days_for(part_id: str) -> int:
    supplier_id = contracts.preferred_supplier(part_id)
    if supplier_id is None:
        return DEFAULT_LEAD_DAYS
    return contracts.contracted_lead_days(part_id, supplier_id) or DEFAULT_LEAD_DAYS


@cache
def _history_cohorts() -> dict[tuple[str, str], str]:
    """Assign the short-history cohorts to quiet pairs.

    The seasonal shrinkage term is `min(years/3, 1)` — exactly 1.0 at 36 months — so without
    pairs that have *less* than the full window, neither shrinkage nor the LOW-confidence path
    ever executes and two of E1's three branches are untestable.

    Short history goes only to pairs with no planted finding: a finding that cannot be detected
    because its own history was truncated would be indistinguishable from a detector bug.
    """
    quiet = sorted(
        (r["part_id"], r["warehouse_id"])
        for r in pair_universe()
        if not expect_finding(r["part_id"], r["warehouse_id"])
        and not r["finding_ids"]  # exclude F9's intermittent spares too
    )
    assignment: dict[tuple[str, str], str] = {}
    for pair in quiet[:SHRUNK_HISTORY_PAIRS]:
        assignment[pair] = "shrunk"
    for pair in quiet[SHRUNK_HISTORY_PAIRS : SHRUNK_HISTORY_PAIRS + SPARSE_HISTORY_PAIRS]:
        assignment[pair] = "sparse"
    return assignment


@cache
def pair_params() -> dict[tuple[str, str], PairParams]:
    """True generative parameters for every pair in the universe."""
    cohorts = _history_cohorts()
    regime_cycle = _build_regime_cycle()
    f7, f8, f9, f6 = FINDINGS["F7"], FINDINGS["F8"], FINDINGS["F9"], FINDINGS["F6"]
    dead_pairs = {tuple(p) for p in f8["pairs"]}

    out: dict[tuple[str, str], PairParams] = {}
    for index, row in enumerate(pair_universe()):
        part_id, warehouse_id = row["part_id"], row["warehouse_id"]
        key = (part_id, warehouse_id)
        findings = tuple(row["finding_ids"])

        regime = regime_cycle[index % len(regime_cycle)]
        level = _base_level(warehouse_id, row["bom_level"], index)
        step_day: int | None = None
        step_factor = 1.0
        quiet_since: int | None = None
        mean_interval = 0.0
        mean_size = 0.0
        seasonal_amplitude = 0.0
        trend_per_day = 0.0

        cohort = cohorts.get(key, "full")
        history_days = HISTORY_DEPTH_COHORTS[cohort]["days"]

        # --- catalog overrides, which win over the rotating assignment -----
        if part_id in f9["parts"] and warehouse_id == f9["warehouse"]:
            regime = "intermittent"
            mean_interval = float(f9["mean_interval_days"])
            mean_size = float(f9["mean_issue_size"])
        elif key in dead_pairs:
            regime = "smooth"
            quiet_since = history_days - f8["quiet_days"]
        elif part_id == f7["part"] and warehouse_id == f7["warehouse"]:
            regime = "step"
            step_day = history_days - f7["step_days_ago"]
            step_factor = float(f7["step_factor"])
        elif part_id == f6["part"]:
            # 2/day is what makes MOQ 500 a ~7-month overbuy rather than an abstract one.
            regime = "smooth"
            level = float(f6["target_daily_burn"])

        # --- regime-specific parameters ------------------------------------
        if regime == "seasonal":
            seasonal_amplitude = 0.35
        elif regime == "trending":
            # +/- ~15%/year, direction fixed by position
            trend_per_day = 0.0004 if index % 2 == 0 else -0.0004
        elif regime == "step" and step_day is None:
            step_day = history_days - int(90 + (index % 5) * 30)
            step_factor = 1.5 if index % 2 == 0 else 0.65
        elif regime == "intermittent" and mean_interval == 0.0:
            mean_interval = float(10 + (index % 3) * 4)
            mean_size = max(1.0, round(level * mean_interval / 3, 1))

        noise_cv = _NOISE_CV[regime]

        # A "smooth" series at under ~5 units/day is not smooth once quantities are integers --
        # rounding dominates and the recorded regime stops describing the data. Slow movers get
        # the intermittent model instead, which is what they actually are.
        if regime != "intermittent" and level < MIN_SMOOTH_LEVEL:
            regime = "intermittent"
            noise_cv = _NOISE_CV["intermittent"]
            mean_interval = float(10 + (index % 3) * 4)
            mean_size = max(1.0, round(level * mean_interval, 1))
            seasonal_amplitude = 0.0
            trend_per_day = 0.0
            step_day = None
            step_factor = 1.0

        # Stock policy is set from the PRE-step level on purpose: that is what makes F7's
        # miscalibration real rather than asserted. A safety stock computed from today's
        # (already shifted) rate would be correctly sized and there would be nothing to find.
        lead_days = _lead_days_for(part_id)
        _, sigma_lead = (
            contracts.true_lead_parameters(contracts.preferred_supplier(part_id), lead_days)
            if contracts.preferred_supplier(part_id)
            else (lead_days, 4.0)
        )
        target_cover = policy.target_cover_days(lead_days, sigma_lead, row["criticality_class"])
        safety_days = max(
            MIN_SAFETY_DAYS, policy.z_for(row["criticality_class"]) * sigma_lead
        )

        out[key] = PairParams(
            part_id=part_id,
            warehouse_id=warehouse_id,
            regime=regime,
            level=level,
            noise_cv=noise_cv,
            history_days=history_days,
            history_cohort=cohort,
            seasonal_amplitude=seasonal_amplitude,
            trend_per_day=trend_per_day,
            step_day=step_day,
            step_factor=step_factor,
            mean_interval_days=mean_interval,
            mean_issue_size=mean_size,
            quiet_since_day=quiet_since,
            safety_stock=max(1, round(level * safety_days)),
            max_stock=max(2, round(level * target_cover)),
            unit_cost=float(row["unit_cost"]),
            criticality_class=row["criticality_class"],
            finding_ids=findings,
        )

    return out


def _multiplicative_noise(rng: np.random.Generator, cv: float, n: int) -> np.ndarray:
    """Mean-1 multiplicative noise at the requested coefficient of variation.

    Normal below CV 0.4, lognormal above it. A normal at CV 0.9 puts ~13% of draws below zero,
    and clipping those to zero truncates the left tail — which drops the realised CV well below
    the target (measured 0.59 against 0.90) and makes the erratic regime look tamer than
    intended. Lognormal is non-negative by construction and right-skewed, which is also the
    shape real erratic demand takes.
    """
    if cv < 0.4:
        return rng.normal(1.0, cv, size=n)
    sigma = np.sqrt(np.log(1.0 + cv**2))
    return rng.lognormal(mean=-(sigma**2) / 2.0, sigma=sigma, size=n)


def _seasonal_factor(day_of_year: np.ndarray, amplitude: float) -> np.ndarray:
    """Festive-season ramp plus a fiscal-year-end push."""
    festive = np.cos(2 * np.pi * (day_of_year - _FESTIVE_PEAK_DOY) / 365.0)
    fiscal = np.cos(2 * np.pi * (day_of_year - _FISCAL_PEAK_DOY) / 365.0)
    return 1.0 + amplitude * (0.7 * festive + 0.3 * fiscal)


def issue_series(params: PairParams, start_day_of_year: int | None = None) -> np.ndarray:
    """Daily ISSUE quantities over the pair's history window, oldest first.

    Returns integers — you cannot issue 3.7 units of a physical part, and rounding at the end
    rather than the beginning keeps the mean unbiased.

    The seasonal phase is derived from the series length, because every window ends today: a
    fixed start-of-year would put a 60-day series' festive peak in the wrong month.
    """
    n = params.history_days
    if start_day_of_year is None:
        start_day_of_year = dates.start_day_of_year(n)
    days = np.arange(n)
    doy = (start_day_of_year + days) % 365

    if params.regime == "intermittent":
        return _croston_series(params, n)

    level = np.full(n, params.level, dtype=float)

    if params.trend_per_day:
        level *= 1.0 + params.trend_per_day * days

    if params.step_day is not None:
        level = np.where(days >= params.step_day, level * params.step_factor, level)

    if params.seasonal_amplitude:
        level *= _seasonal_factor(doy, params.seasonal_amplitude)

    rng = _rng(params.part_id, params.warehouse_id, "issues")
    noise = _multiplicative_noise(rng, params.noise_cv, n)
    series = np.clip(level * noise, 0.0, None)

    # Plant stores follow the production calendar; RDCs ship every day.
    if params.warehouse_id in ("WH001", "WH002"):
        weekday = (days + 4) % 7  # arbitrary but fixed phase
        series = np.where(weekday >= 5, 0.0, series)

    if params.quiet_since_day is not None:
        series = np.where(days >= params.quiet_since_day, 0.0, series)

    return np.rint(series).astype(int)


def _croston_series(params: PairParams, n: int) -> np.ndarray:
    """Lumpy demand: issues at random intervals, sized independently of the gap.

    This is the shape a daily mean cannot describe — the whole reason E1 tests for intermittency
    before doing anything else.
    """
    rng = _rng(params.part_id, params.warehouse_id, "croston")
    series = np.zeros(n, dtype=int)
    day = int(rng.integers(0, max(1, round(params.mean_interval_days))))
    while day < n:
        size = max(1, round(float(rng.exponential(params.mean_issue_size))))
        series[day] = size
        gap = max(1, round(float(rng.exponential(params.mean_interval_days))))
        day += gap
    if params.quiet_since_day is not None:
        series[params.quiet_since_day :] = 0
    return series


def realised_daily_mean(params: PairParams) -> float:
    """Mean daily issue actually realised — the number E1 is trying to recover."""
    return float(issue_series(params).mean())
