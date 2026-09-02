"""The eight phase-1 UC functions, as thin reads over `inventory_signal_board`.

Where the old `deep_analysis_functions.ipynb` had 16 scalar/table functions
each doing its own table scan, every function here reads the board — nothing
recomputes a nuance the board already carries. One source of truth: if the
board says a part has network surplus, `scan_transfer_options` cannot
disagree with it.

Deliberately excluded (see docs/market_evidence_phase1.md §7 / conversation):
`classify_urgency`, `predicted_stockout_date`, `requested_restock_qty`,
`pending_procurement_qty`, `dynamic_reorder_point`, `consumption_anomaly_score`
and friends from the old 16 are now board *columns*, not callable functions —
calling a scalar function per row does not survive past a few hundred
part/warehouse pairs (see signal_board.py docstring). `assembly_risk_report`,
`financial_tradeoff_summary`, `plant_capacity_check` returned narrative
STRINGs, which was a workaround for not having an LLM in the loop; the board
carries the numbers as columns and Genie writes the sentence instead.

Function 3, `rank_priority_actions`, is the one that matters most and the
one whose ranking has NOT been validated end to end yet (the open item from
docs/market_evidence_phase1.md §16: does decision-value ranking actually
differ from raw exposure on real data, or does it collapse back to the same
ordering). Its formula is intentionally simple and visible rather than
hidden in a black box, specifically so it can be argued with:

    exposure       = value_at_risk (cascade) OR shortfall_qty * unit_cost (direct shortage)
    action_cost    = exposure * 0.03                          if a transfer covers it
                   = exposure * (0.15 .. 0.50, scaled by lead time)   if only a buy exists
                   = exposure * 1.00                            if no fix exists at all
    decision_value = GREATEST(exposure - action_cost, 0)

Suppression (nuance 8) is applied inside rank_priority_actions as a WHERE
clause, not left to Genie to notice: an LLM that gets it right 97% of the
time re-raises a rejected item roughly monthly. An open commitment on the
same part/warehouse suppresses the row -- but only while it is fresh.
PENDING_APPROVAL / NEEDS_REVIEW re-surface after 2 days (a defensible PM
turnaround) and APPROVED / FULFILLING after effective_lead_days + 3
(execution plus a grace buffer), tagged STALLED_COMMITMENT, because their
exposure keeps accruing while they sit. REJECTED matches neither branch and
stays permanently suppressed — a closed decision.

Known simplification: that re-surfacing is time-based only. The "exposure
grew 1.5x since the last decision" rule from docs/market_evidence_phase1.md
needs an EXPOSURE_AT_DECISION value captured at decision time, which
fact_restock_request does not currently store — a real follow-up, not
implemented here.
"""

from agentic_restock.config import qualified_table
from agentic_restock.jobs.signal_board import BOARD_TABLE_NAME

FUNCTION_NAMES = [
    "scan_transfer_options",
    "scan_assembly_risk",
    "rank_priority_actions",
    "rank_priority_actions_diverse",
    "scan_demand_shift",
    "scan_leadtime_drift",
    "evaluate_suppliers",
    "evaluate_feasibility",
]


def build_function_statements(
    app_catalog: str | None = None,
    app_schema: str | None = None,
    gold_catalog: str | None = None,
    facts_schema: str | None = None,
) -> list[str]:
    """Return the eight `CREATE OR REPLACE FUNCTION` statements, in dependency
    order (none actually depend on each other, but this is a stable reading
    order matching docs/market_evidence_phase1.md §7).
    """
    board = qualified_table(BOARD_TABLE_NAME, app_catalog, app_schema)
    contract = qualified_table("dim_supplier_contract", app_catalog, app_schema)
    delivery_full = f"{gold_catalog or 'gold_dev'}.{facts_schema or 'supply_chain_analytics'}.fact_supplier_delivery"
    quality_full = f"{gold_catalog or 'gold_dev'}.{facts_schema or 'supply_chain_analytics'}.fact_supplier_quality"
    supplier_full = f"{gold_catalog or 'gold_dev'}.dim.dim_supplier"
    func_prefix = qualified_table("", app_catalog, app_schema).rstrip(".")

    scan_transfer_options = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.scan_transfer_options(
        min_value DOUBLE DEFAULT 0,
        part_id_filter STRING DEFAULT NULL
    )
    RETURNS TABLE (
        part_id STRING, warehouse_id STRING, on_hand INT, safety_stock INT,
        shortfall_qty INT, best_donor_warehouse_id STRING, network_surplus_qty INT,
        donor_cover_after_units INT, transfer_value DOUBLE
    )
    COMMENT 'Nuance 1: parts short at one warehouse with real, donor-protected surplus at another. transfer_value is what buying the shortfall would have cost -- the money a transfer avoids spending, not just moving stock around.'
    RETURN
    SELECT
        part_id, warehouse_id, on_hand, safety_stock,
        (safety_stock - on_hand) AS shortfall_qty,
        best_donor_warehouse_id, network_surplus_qty, donor_cover_after_units,
        LEAST(network_surplus_qty, safety_stock - on_hand) * unit_cost AS transfer_value
    FROM {board}
    WHERE on_hand < safety_stock
      AND best_donor_warehouse_id IS NOT NULL
      AND (part_id_filter IS NULL OR part_id = part_id_filter)
      AND LEAST(network_surplus_qty, safety_stock - on_hand) * unit_cost >= min_value
    ORDER BY transfer_value DESC
    """.strip()

    scan_assembly_risk = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.scan_assembly_risk(
        min_value DOUBLE DEFAULT 0,
        part_id_filter STRING DEFAULT NULL
    )
    RETURNS TABLE (
        component_part_id STRING, warehouse_id STRING,
        threatened_parent_part_id STRING, parent_units_blocked INT, value_at_risk DOUBLE
    )
    COMMENT 'Nuance 2: components healthy on their own that still block an A-CRITICAL parent build target. value_at_risk is parent_units_blocked * parent unit cost.'
    RETURN
    SELECT
        part_id AS component_part_id, warehouse_id,
        threatened_parent_part_id, parent_units_blocked, value_at_risk
    FROM {board}
    WHERE threatened_parent_part_id IS NOT NULL
      AND value_at_risk >= min_value
      AND (part_id_filter IS NULL OR part_id = part_id_filter)
    ORDER BY value_at_risk DESC
    """.strip()

    rank_priority_actions = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.rank_priority_actions(
        max_rows INT DEFAULT 5
    )
    RETURNS TABLE (
        part_id STRING, warehouse_id STRING, signal_type STRING,
        exposure DOUBLE, action_cost DOUBLE, decision_value DOUBLE,
        best_donor_warehouse_id STRING, network_surplus_qty INT,
        threatened_parent_part_id STRING, preferred_supplier_id STRING,
        effective_lead_days DOUBLE, reliability_score DOUBLE,
        commitment_state STRING, commitment_age_days INT
    )
    COMMENT 'Nuance 3: ranks by what changes if a human acts (exposure minus the cost of the cheapest viable fix), not by raw exposure. Open commitments (pending, approved, fulfilling) are suppressed inside this function -- not left for Genie to notice -- because suppression must hold every time, not most of the time. REJECTED stays permanently suppressed (a closed decision). PENDING_APPROVAL/NEEDS_REVIEW/APPROVED/FULFILLING are only suppressed while fresh -- once one sits longer than a defensible turnaround (2 days for a PM decision; the part''s own lead time plus a 3-day grace buffer for execution), it re-surfaces here as STALLED_COMMITMENT because its exposure is still accruing. See module docstring for the exposure/action_cost/decision_value formula and its known simplifications.'
    RETURN
    WITH scored AS (
        SELECT
            part_id, warehouse_id,
            commitment_state,
            CASE
                WHEN commitment_state IN ('PENDING_APPROVAL', 'NEEDS_REVIEW')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_requested_date_key AS STRING), 'yyyyMMdd'))
                WHEN commitment_state IN ('APPROVED', 'FULFILLING')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_decision_date_key AS STRING), 'yyyyMMdd'))
                ELSE NULL
            END AS commitment_age_days,
            CASE
                WHEN commitment_state IN ('PENDING_APPROVAL', 'NEEDS_REVIEW')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_requested_date_key AS STRING), 'yyyyMMdd')) > 2
                WHEN commitment_state IN ('APPROVED', 'FULFILLING')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_decision_date_key AS STRING), 'yyyyMMdd')) > COALESCE(effective_lead_days, 30) + 3
                ELSE FALSE
            END AS is_stale_commitment,
            CASE
                WHEN threatened_parent_part_id IS NOT NULL THEN 'BOM_CASCADE_RISK'
                WHEN on_hand < safety_stock THEN 'STOCK_THRESHOLD'
                ELSE NULL
            END AS base_signal_type,
            COALESCE(
                value_at_risk,
                CASE WHEN on_hand < safety_stock THEN (safety_stock - on_hand) * unit_cost ELSE NULL END
            ) AS exposure,
            (best_donor_warehouse_id IS NOT NULL
                AND network_surplus_qty >= GREATEST(safety_stock - on_hand, 0)) AS has_transfer_fix,
            (preferred_supplier_id IS NOT NULL) AS has_buy_fix,
            best_donor_warehouse_id, network_surplus_qty,
            threatened_parent_part_id, preferred_supplier_id,
            effective_lead_days, reliability_score
        FROM {board}
    ),
    exposed AS (
        SELECT *,
            CASE WHEN is_stale_commitment THEN 'STALLED_COMMITMENT' ELSE base_signal_type END AS signal_type,
            CASE
                WHEN has_transfer_fix THEN exposure * 0.03
                WHEN has_buy_fix THEN exposure * (0.15 + LEAST(COALESCE(effective_lead_days, 90), 90) / 90.0 * 0.35)
                ELSE exposure
            END AS action_cost
        FROM scored
        WHERE exposure IS NOT NULL AND exposure > 0
          -- Suppression (nuance 8): an open commitment on this exact
          -- part/warehouse means a human is already acting on it, unless it
          -- has overstayed its state (is_stale_commitment) -- then it must
          -- resurface rather than stay invisible while exposure accrues.
          -- REJECTED never matches either branch, so it stays permanently
          -- suppressed, same as before.
          AND (commitment_state IS NULL OR is_stale_commitment)
    ),
    -- A SQL UDF's LIMIT clause must be a literal, not a function parameter
    -- (Databricks: INVALID_LIMIT_LIKE_EXPRESSION.IS_UNFOLDABLE), and QUALIFY
    -- cannot see a UDF parameter either (UNRESOLVED_COLUMN once nested past
    -- two CTEs). A plain WHERE on a pre-ranked column accepts the parameter
    -- fine, so the cap is applied that way instead.
    ranked AS (
        SELECT *,
            GREATEST(exposure - action_cost, 0) AS decision_value,
            ROW_NUMBER() OVER (ORDER BY GREATEST(exposure - action_cost, 0) DESC) AS rn
        FROM exposed
    )
    SELECT
        part_id, warehouse_id, signal_type,
        exposure, action_cost, decision_value,
        best_donor_warehouse_id, network_surplus_qty,
        threatened_parent_part_id, preferred_supplier_id,
        effective_lead_days, reliability_score,
        commitment_state, commitment_age_days
    FROM ranked
    WHERE rn <= max_rows
    ORDER BY decision_value DESC
    """.strip()

    rank_priority_actions_diverse = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.rank_priority_actions_diverse(
        min_value DOUBLE DEFAULT 0
    )
    RETURNS TABLE (
        part_id STRING, warehouse_id STRING, signal_type STRING,
        exposure DOUBLE, action_cost DOUBLE, decision_value DOUBLE,
        best_donor_warehouse_id STRING, network_surplus_qty INT,
        threatened_parent_part_id STRING, preferred_supplier_id STRING,
        effective_lead_days DOUBLE, reliability_score DOUBLE,
        commitment_state STRING, commitment_age_days INT
    )
    COMMENT 'Nuance 3b: the same ranking as rank_priority_actions, but returns the top-ranked row PER signal_type instead of one global top-N -- so a run surfaces the best STOCK_THRESHOLD, BOM_CASCADE_RISK, and STALLED_COMMITMENT action side by side (whichever are currently live) instead of only the single loudest number. Naturally bounded by the number of distinct signal_type values (3 today), so no runtime LIMIT/QUALIFY-on-parameter is needed -- see rank_priority_actions'' comment on why that would be awkward in a SQL UDF anyway. Same exposure/action_cost/decision_value formula and nuance-8 suppression as rank_priority_actions -- see that function''s comment and the module docstring.'
    RETURN
    WITH scored AS (
        SELECT
            part_id, warehouse_id,
            commitment_state,
            CASE
                WHEN commitment_state IN ('PENDING_APPROVAL', 'NEEDS_REVIEW')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_requested_date_key AS STRING), 'yyyyMMdd'))
                WHEN commitment_state IN ('APPROVED', 'FULFILLING')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_decision_date_key AS STRING), 'yyyyMMdd'))
                ELSE NULL
            END AS commitment_age_days,
            CASE
                WHEN commitment_state IN ('PENDING_APPROVAL', 'NEEDS_REVIEW')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_requested_date_key AS STRING), 'yyyyMMdd')) > 2
                WHEN commitment_state IN ('APPROVED', 'FULFILLING')
                    THEN DATEDIFF(CURRENT_DATE(), to_date(CAST(open_commitment_decision_date_key AS STRING), 'yyyyMMdd')) > COALESCE(effective_lead_days, 30) + 3
                ELSE FALSE
            END AS is_stale_commitment,
            CASE
                WHEN threatened_parent_part_id IS NOT NULL THEN 'BOM_CASCADE_RISK'
                WHEN on_hand < safety_stock THEN 'STOCK_THRESHOLD'
                ELSE NULL
            END AS base_signal_type,
            COALESCE(
                value_at_risk,
                CASE WHEN on_hand < safety_stock THEN (safety_stock - on_hand) * unit_cost ELSE NULL END
            ) AS exposure,
            (best_donor_warehouse_id IS NOT NULL
                AND network_surplus_qty >= GREATEST(safety_stock - on_hand, 0)) AS has_transfer_fix,
            (preferred_supplier_id IS NOT NULL) AS has_buy_fix,
            best_donor_warehouse_id, network_surplus_qty,
            threatened_parent_part_id, preferred_supplier_id,
            effective_lead_days, reliability_score
        FROM {board}
    ),
    exposed AS (
        SELECT *,
            CASE WHEN is_stale_commitment THEN 'STALLED_COMMITMENT' ELSE base_signal_type END AS signal_type,
            CASE
                WHEN has_transfer_fix THEN exposure * 0.03
                WHEN has_buy_fix THEN exposure * (0.15 + LEAST(COALESCE(effective_lead_days, 90), 90) / 90.0 * 0.35)
                ELSE exposure
            END AS action_cost
        FROM scored
        WHERE exposure IS NOT NULL AND exposure > 0
          AND (commitment_state IS NULL OR is_stale_commitment)
    ),
    ranked AS (
        SELECT *,
            GREATEST(exposure - action_cost, 0) AS decision_value,
            ROW_NUMBER() OVER (PARTITION BY signal_type ORDER BY GREATEST(exposure - action_cost, 0) DESC) AS rn_in_type
        FROM exposed
    )
    SELECT
        part_id, warehouse_id, signal_type,
        exposure, action_cost, decision_value,
        best_donor_warehouse_id, network_surplus_qty,
        threatened_parent_part_id, preferred_supplier_id,
        effective_lead_days, reliability_score,
        commitment_state, commitment_age_days
    FROM ranked
    WHERE rn_in_type = 1 AND decision_value >= min_value
    ORDER BY decision_value DESC
    """.strip()

    scan_demand_shift = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.scan_demand_shift(
        part_id_filter STRING DEFAULT NULL
    )
    RETURNS TABLE (
        part_id STRING, warehouse_id STRING,
        flat_daily_burn DOUBLE, seasonal_multiplier DOUBLE, adj_daily_burn DOUBLE, days_of_cover DOUBLE
    )
    COMMENT 'Nuance 4: parts whose seasonally-adjusted burn rate materially disagrees with the flat trailing average (multiplier outside 0.8-1.2x) -- the correction every other ranking in this module inherits if it is wrong.'
    RETURN
    SELECT part_id, warehouse_id, flat_daily_burn, seasonal_multiplier, adj_daily_burn, days_of_cover
    FROM {board}
    WHERE (seasonal_multiplier >= 1.2 OR seasonal_multiplier <= 0.8)
      AND (part_id_filter IS NULL OR part_id = part_id_filter)
    ORDER BY ABS(seasonal_multiplier - 1.0) DESC
    """.strip()

    scan_leadtime_drift = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.scan_leadtime_drift(
        min_days DOUBLE DEFAULT 3,
        part_id_filter STRING DEFAULT NULL
    )
    RETURNS TABLE (
        part_id STRING, preferred_supplier_id STRING,
        contracted_lead_days INT, observed_avg_delay_days DOUBLE,
        effective_lead_days DOUBLE, otd_rate DOUBLE, reliability_score DOUBLE
    )
    COMMENT 'Nuance 5: parts whose contracted lead time and observed delivery reality have drifted apart by at least min_days -- the stale-master-data signal, one row per part (lead time is a supplier property, not a per-warehouse one).'
    RETURN
    SELECT DISTINCT
        part_id, preferred_supplier_id, contracted_lead_days,
        observed_avg_delay_days, effective_lead_days, otd_rate, reliability_score
    FROM {board}
    WHERE observed_avg_delay_days >= min_days
      AND (part_id_filter IS NULL OR part_id = part_id_filter)
    ORDER BY observed_avg_delay_days DESC
    """.strip()

    evaluate_suppliers = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.evaluate_suppliers(
        part_id_filter STRING
    )
    RETURNS TABLE (
        supplier_id STRING, lead_time_days INT, moq INT, pack_size INT,
        unit_cost DOUBLE, is_preferred BOOLEAN, otd_rate DOUBLE, reliability_score DOUBLE
    )
    COMMENT 'Nuance 6: every contracted supplier for a part, with observed reliability -- not just the preferred one the board carries -- so a switch can be justified against the incumbent.'
    RETURN
    WITH delivery_stats AS (
        SELECT SUPPLIER_KEY,
            AVG(CASE WHEN OTD_FLAG THEN 1.0 ELSE 0.0 END) AS otd_rate,
            AVG(DELAY_DAYS) AS avg_delay
        FROM {delivery_full}
        GROUP BY SUPPLIER_KEY
    ),
    quality_stats AS (
        SELECT SUPPLIER_KEY, AVG(PPM_LEVEL) AS avg_ppm
        FROM {quality_full}
        GROUP BY SUPPLIER_KEY
    )
    SELECT
        c.supplier_id, c.lead_time_days, c.moq, c.pack_size, c.unit_cost, c.is_preferred,
        ds.otd_rate,
        GREATEST(0, LEAST(100,
            (COALESCE(ds.otd_rate, 0.9) * 70) + (100 - LEAST(COALESCE(qs.avg_ppm, 0) / 100.0, 30)) * 0.3
        )) AS reliability_score
    FROM {contract} c
    LEFT JOIN {supplier_full} sup ON c.supplier_id = sup.SUPPLIER_ID AND sup.IS_CURRENT = true
    LEFT JOIN delivery_stats ds ON sup.SUPPLIER_KEY = ds.SUPPLIER_KEY
    LEFT JOIN quality_stats qs ON sup.SUPPLIER_KEY = qs.SUPPLIER_KEY
    WHERE c.part_id = part_id_filter
    ORDER BY reliability_score DESC
    """.strip()

    evaluate_feasibility = f"""
    CREATE OR REPLACE FUNCTION {func_prefix}.evaluate_feasibility(
        part_id_filter STRING,
        supplier_id_filter STRING,
        ideal_qty INT
    )
    RETURNS TABLE (
        feasible_qty INT, moq INT, pack_size INT, excess_qty INT, excess_holding_cost DOUBLE
    )
    COMMENT 'Nuance 7: rounds an ideal shortfall up to what the supplier will actually accept (MOQ, pack multiples), and prices the excess this creates -- so a recommendation is never one the customer cannot place.'
    RETURN
    SELECT
        GREATEST(
            moq,
            CAST(CEIL(ideal_qty / CAST(pack_size AS DOUBLE)) * pack_size AS INT)
        ) AS feasible_qty,
        moq, pack_size,
        GREATEST(
            moq,
            CAST(CEIL(ideal_qty / CAST(pack_size AS DOUBLE)) * pack_size AS INT)
        ) - ideal_qty AS excess_qty,
        (GREATEST(
            moq,
            CAST(CEIL(ideal_qty / CAST(pack_size AS DOUBLE)) * pack_size AS INT)
        ) - ideal_qty) * unit_cost * 0.02 AS excess_holding_cost
    FROM {contract}
    WHERE part_id = part_id_filter AND supplier_id = supplier_id_filter
    """.strip()

    return [
        scan_transfer_options,
        scan_assembly_risk,
        rank_priority_actions,
        rank_priority_actions_diverse,
        scan_demand_shift,
        scan_leadtime_drift,
        evaluate_suppliers,
        evaluate_feasibility,
    ]
