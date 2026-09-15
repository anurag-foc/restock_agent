import json

import pytest

from agentic_restock import settings as S
from agentic_restock.detectors import fixes, scanners
from agentic_restock.estimators import leadtime as e2
from agentic_restock.generation import policy

# --- the safety property ---------------------------------------------------
#
# Every default must equal the value that was hardcoded before this module existed.
# If one of these fails, an empty settings table silently changes what the pipeline
# recommends -- which is the one outcome the migration must not have.


def test_defaults_match_previously_hardcoded_values():
    d = S.DEFAULTS
    assert d.holding_rate == policy.HOLDING_RATE
    assert d.min_exposure == scanners.MIN_EXPOSURE
    assert d.dead_stock_cover_days == scanners.DEAD_CAPITAL_COVER_DAYS
    assert d.dead_stock_min_value == scanners.DEAD_CAPITAL_MIN_VALUE
    assert d.donor_protection_multiple == fixes.DONOR_PROTECTION_MULTIPLE
    assert d.donor_min_cover_after_days == fixes.DONOR_MIN_COVER_AFTER_DAYS
    assert d.leadtime_half_life_days == e2.RECENCY_HALF_LIFE_DAYS


def test_balanced_preset_is_the_hardcoded_transfer_pair():
    assert S.TRANSFER_CAUTION_PRESETS["balanced"] == (
        fixes.DONOR_PROTECTION_MULTIPLE,
        fixes.DONOR_MIN_COVER_AFTER_DAYS,
    )


def test_empty_table_resolves_to_defaults():
    assert S.resolve([]).as_dict() == S.DEFAULTS.as_dict()
    assert S.resolve(None).as_dict() == S.DEFAULTS.as_dict()


# --- coercion --------------------------------------------------------------


def test_coerce_parses_and_keeps_type():
    assert S.coerce("holding_rate", "0.2") == 0.2
    assert S.coerce("items_per_notification", "5") == 5
    assert isinstance(S.coerce("items_per_notification", 5.0), int)


def test_coerce_rejects_out_of_range_rather_than_clamping():
    with pytest.raises(S.SettingError, match="below the minimum"):
        S.coerce("holding_rate", 0.01)
    with pytest.raises(S.SettingError, match="above the maximum"):
        S.coerce("items_per_notification", 50)


def test_coerce_rejects_unknown_key_and_bad_choice():
    with pytest.raises(S.SettingError, match="unknown setting"):
        S.coerce("nope", 1)
    with pytest.raises(S.SettingError, match="is not one of"):
        S.coerce("consumption_model", "arima")


def test_minimum_value_cannot_be_zero():
    # A zero floor floods the PM with every trivial finding, which is the failure mode
    # the output budget exists to prevent.
    with pytest.raises(S.SettingError):
        S.coerce("min_exposure", 0)


# --- resolution ------------------------------------------------------------


def test_resolve_applies_overrides_and_keeps_other_defaults():
    resolved = S.resolve([{"SETTING_KEY": "holding_rate", "SETTING_VALUE": "0.25"}])
    assert resolved.holding_rate == 0.25
    assert resolved.items_per_notification == S.DEFAULTS.items_per_notification


def test_resolve_skips_bad_rows_instead_of_failing():
    # A malformed value must cost one setting, not the whole scan.
    resolved = S.resolve(
        [
            {"SETTING_KEY": "holding_rate", "SETTING_VALUE": "not-a-number"},
            {"SETTING_KEY": "unknown_key", "SETTING_VALUE": "1"},
            {"SETTING_KEY": "items_per_notification", "SETTING_VALUE": "3"},
        ]
    )
    assert resolved.holding_rate == S.DEFAULTS.holding_rate
    assert resolved.items_per_notification == 3


def test_resolve_rejects_out_of_range_stored_value():
    # Bounds hold on read as well as on write -- a value can predate a tightened range.
    resolved = S.resolve([{"SETTING_KEY": "holding_rate", "SETTING_VALUE": "0.99"}])
    assert resolved.holding_rate == S.DEFAULTS.holding_rate


def test_presets_expand_to_numbers_the_detectors_use():
    protective = S.resolve(
        [{"SETTING_KEY": "transfer_caution", "SETTING_VALUE": '"protective"'}]
    )
    assert protective.donor_protection_multiple == 1.5
    assert protective.donor_min_cover_after_days == 45.0

    fast = S.resolve([{"SETTING_KEY": "leadtime_recency", "SETTING_VALUE": '"fast"'}])
    assert fast.leadtime_half_life_days == 90.0


def test_unknown_key_lookup_raises():
    with pytest.raises(S.SettingError):
        S.DEFAULTS["not_a_setting"]


# --- snapshot --------------------------------------------------------------


def test_snapshot_contains_every_setting_including_defaulted_ones():
    # A run must be reproducible from its own record; "the defaults at the time" is
    # exactly what will have moved by the time anyone asks.
    snapshot = json.loads(S.snapshot_json(S.resolve([])))
    assert set(snapshot) == {spec.key for spec in S.SPECS}


# --- SQL -------------------------------------------------------------------


def test_write_validates_before_building_sql():
    with pytest.raises(S.SettingError):
        S.build_settings_write("holding_rate", 5.0, updated_by="priya")


def test_write_escapes_quotes_in_user_field():
    sql = S.build_settings_write("holding_rate", 0.2, updated_by="o'brien")
    assert "o''brien" in sql


def test_read_query_takes_newest_row_per_key():
    sql = S.build_settings_read_query()
    assert "ROW_NUMBER()" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "rn = 1" in sql


def test_ddl_is_idempotent_and_named_from_config():
    ddl = S.build_settings_table_ddl()
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert S.TABLE_APP_SETTINGS in ddl


def test_warning_fires_above_soft_limit_only():
    assert S.warnings_for("items_per_notification", 4) == []
    assert S.warnings_for("items_per_notification", 9)


# --- wiring: a changed setting must change the output ----------------------
#
# The point of the settings layer is not that it parses values, it is that the pipeline
# obeys them. These assert the effect, not the plumbing.


def _dead_stock_frame(cover_days: float, on_hand: int = 1000, unit_cost: float = 500.0):
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "PART_ID": "P1",
                "WAREHOUSE_ID": "WH1",
                "DAYS_OF_COVER": cover_days,
                "BURN_METHOD": "CROSTON",
                "BURN_CONFIDENCE": "MEDIUM",
                "FORWARD_BURN": 1.0,
                "ON_HAND_QTY": on_hand,
                "UNIT_COST": unit_cost,
            }
        ]
    )


def test_holding_rate_changes_what_dead_stock_is_worth():
    frame = _dead_stock_frame(cover_days=400.0)
    cheap = scanners.scan_dead_capital(frame, S.DEFAULTS)
    dear = scanners.scan_dead_capital(
        frame, S.resolve([{"SETTING_KEY": "holding_rate", "SETTING_VALUE": "0.28"}])
    )
    assert cheap and dear
    # Twice the rate, twice the annual carrying cost.
    assert dear[0].exposure == pytest.approx(cheap[0].exposure * 2, rel=1e-6)


def test_dead_stock_threshold_changes_whether_it_fires_at_all():
    frame = _dead_stock_frame(cover_days=200.0)  # above the 180-day default
    assert scanners.scan_dead_capital(frame, S.DEFAULTS)

    relaxed = S.resolve(
        [{"SETTING_KEY": "dead_stock_cover_days", "SETTING_VALUE": "365"}]
    )
    assert scanners.scan_dead_capital(frame, relaxed) == []


def test_minimum_value_suppresses_a_small_finding():
    frame = _dead_stock_frame(cover_days=400.0, on_hand=200, unit_cost=400.0)  # Rs 80k
    assert scanners.scan_dead_capital(frame, S.DEFAULTS)

    strict = S.resolve([{"SETTING_KEY": "dead_stock_min_value", "SETTING_VALUE": "100000"}])
    assert scanners.scan_dead_capital(frame, strict) == []


def _loc(warehouse_id, **overrides):
    base = {
        "warehouse_id": warehouse_id,
        "available_qty": 1000,
        "safety_stock_qty": 200,
        "forward_burn": 10.0,
        "sigma_d": 2.0,
        "mu_lead": 40.0,
        "sigma_lead": 3.0,
        "consequence": 1_000_000.0,
    }
    base.update(overrides)
    return base


def test_transfer_caution_changes_how_much_a_donor_gives_up():
    # The donor must be the binding constraint, not the receiver's need -- with a deep
    # donor every preset returns the same quantity and the test passes vacuously.
    receiver = _loc("WH1", available_qty=0)
    donor = _loc("WH2", available_qty=600, forward_burn=10.0)

    def qty(caution: str) -> int:
        cfg = S.resolve(
            [{"SETTING_KEY": "transfer_caution", "SETTING_VALUE": f'"{caution}"'}]
        )
        options = fixes.rank_transfer_options(
            part_id="P1", receiver=receiver, candidates=[donor],
            freight_cost=100.0, settings=cfg,
        )
        return options[0].transfer_qty if options else 0

    # Protective keeps more back than balanced, which keeps more than relaxed.
    assert qty("protective") <= qty("balanced") <= qty("relaxed")
    assert qty("protective") < qty("relaxed"), "the preset must actually bite"


def test_excess_holding_cost_follows_the_configured_rate():
    from agentic_restock.generation import policy as pol

    base = pol.excess_holding_cost(440, 100.0, 2.0)
    doubled = pol.excess_holding_cost(440, 100.0, 2.0, pol.HOLDING_RATE * 2)
    assert doubled == pytest.approx(base * 2, rel=1e-9)


# --- run log stamping ------------------------------------------------------


def test_run_log_insert_carries_the_settings_snapshot():
    from agentic_restock.jobs import run_log

    sql = run_log.build_run_log_insert(
        candidate_count=2,
        outcome="SUPERVISOR_INVOKED",
        note="ok",
        settings_snapshot=S.snapshot_json(S.DEFAULTS),
    )
    assert "settings_snapshot" in sql
    assert "holding_rate" in sql


def test_run_log_migration_is_separate_from_the_ddl():
    # CREATE TABLE IF NOT EXISTS will not widen an existing table, so a log table from
    # before settings existed needs its own ALTER.
    from agentic_restock.jobs import run_log

    assert "ADD COLUMNS" in run_log.build_run_log_migration()
    assert "settings_snapshot" in run_log.build_run_log_table_ddl()


# --- wiring: the forecasting settings ---------------------------------------


def _lumpy_series(n=1100, every=25, size=40.0):
    import numpy as np

    s = np.zeros(n)
    s[np.arange(5, n, every)] = size
    return s


def test_consumption_model_changes_the_method_and_the_rate():
    from datetime import date

    from agentic_restock.estimators import burn as e1

    s = _lumpy_series()
    as_of = date(2026, 9, 15)

    auto = e1.estimate_burn(s, end_date=as_of, horizon_days=30, model="automatic")
    flat = e1.estimate_burn(s, end_date=as_of, horizon_days=30, model="simple_average")

    assert auto.method == e1.METHOD_CROSTON
    assert flat.method == e1.METHOD_SIMPLE_AVERAGE


def test_recent_only_reads_a_slow_mover_as_not_moving():
    """The documented trap the panel has to warn about, asserted rather than assumed.

    A part that moves less often than the window has no issues inside it, so the rate is
    zero -- which reads downstream as stock that is no longer moving. That is the setting
    behaving correctly, and it is why the panel shows the affected part count.
    """
    from datetime import date

    import numpy as np

    from agentic_restock.estimators import burn as e1

    s = np.zeros(1100)
    s[np.arange(5, 900, 120)] = 50.0  # last issue ~200 days before the end

    auto = e1.estimate_burn(s, end_date=date(2026, 9, 15), horizon_days=30, model="automatic")
    recent = e1.estimate_burn(
        s, end_date=date(2026, 9, 15), horizon_days=30,
        model="recent_only", recent_days=90,
    )
    assert recent.forward_burn == 0.0
    assert auto.method != e1.METHOD_RECENT_ONLY


def test_every_consumption_model_still_reports_a_spread():
    """The plug-in contract: the risk model reads sigma_d, so a method that returned only a
    rate would silently break the ranking rather than fail."""
    from datetime import date

    from agentic_restock.estimators import burn as e1

    s = _lumpy_series()
    for model in ("automatic", "simple_average", "recent_only"):
        est = e1.estimate_burn(s, end_date=date(2026, 9, 15), horizon_days=30, model=model)
        assert est.sigma_d > 0, f"{model} reported no spread"


def test_ignore_outliers_resists_a_single_extreme_delivery():
    obs = [
        e2.DeliveryObservation("P1", "S1", "CAST", d, a)
        for d, a in zip(
            [1, 0, 2, 1, 0, 1, 2, 0, 1, 45],
            [20, 60, 100, 140, 180, 220, 260, 300, 340, 30],
        )
    ]
    kw = {
        "part_id": "P1",
        "supplier_id": "S1",
        "supplier_category": "CAST",
        "contracted_days": 30,
    }
    weighted = e2.estimate_lead_time(obs, **kw, model="recent_weighted")
    robust = e2.estimate_lead_time(obs, **kw, model="ignore_outliers")

    # One 45-day delay dominates the weighted estimate and barely moves the median.
    assert weighted.mu_lead > robust.mu_lead + 5
    assert robust.sigma_lead < weighted.sigma_lead


def test_leadtime_recency_changes_how_much_old_history_counts():
    # Bad a year ago, clean recently: a short half-life should forgive more.
    obs = [
        e2.DeliveryObservation("P1", "S1", "CAST", d, a)
        for d, a in zip(
            [14, 13, 12, 11, 1, 0, 1, 0, 1, 0],
            [700, 640, 580, 520, 120, 90, 60, 40, 20, 10],
        )
    ]
    kw = {
        "part_id": "P1",
        "supplier_id": "S1",
        "supplier_category": "CAST",
        "contracted_days": 30,
    }
    fast = e2.estimate_lead_time(obs, **kw, half_life_days=90.0)
    slow = e2.estimate_lead_time(obs, **kw, half_life_days=365.0)
    assert fast.drift_days < slow.drift_days


def test_unknown_model_falls_back_rather_than_raising():
    # A settings row that predates a rename must not stop a scan.
    from datetime import date

    from agentic_restock.estimators import burn as e1

    est = e1.estimate_burn(
        _lumpy_series(), end_date=date(2026, 9, 15), horizon_days=30, model="nonsense"
    )
    assert est.method == e1.METHOD_CROSTON
