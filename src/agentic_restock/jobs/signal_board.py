"""Build the inventory signal board — the phase-1 replacement for the coarse check.

Where the old §4.1 coarse check emitted candidate *rows* filtered by a single
threshold, this emits a *table*: one row per (part, warehouse), covering the
full working set, with columns for every phase-1 intelligence nuance
(docs/market_evidence_phase1.md §7) computed set-wise. Nothing here is a
per-row scalar UDF call — at production volume (hundreds of thousands of
part/warehouse pairs), a scalar function that scans a fact table inside its
body cannot be evaluated per row. Every column below is a window function or
a join, computed once over the whole set.

The board is Genie's read surface. The seven phase-1 UC functions
(notebooks/uc_functions/priority_functions.ipynb) are views over it, not
independent computations — so there is exactly one source of truth for "what
is true about this part/warehouse right now."

Nuance coverage:
  1 network surplus / transfer   -> network_surplus_qty, best_donor_warehouse_id, donor_cover_after_units
  2 BOM cascade value at risk    -> threatened_parent_part_id, value_at_risk
  3 decision-value ranking       -> decision_value (consumed by rank_priority_actions, not computed here)
  4 seasonality-adjusted burn    -> adj_daily_burn, days_of_cover
  5 lead-time reality check      -> contracted_lead_days, observed_avg_delay_days, effective_lead_days
  6 supplier reliability         -> otd_rate, reliability_score
  7 MOQ / pack feasibility       -> left to evaluate_feasibility(part_id, supplier_id, qty); needs a chosen qty

Open validation items (docs/market_evidence_phase1.md §16), not resolved by
this query alone:
  - value_at_risk uses (MAX_STOCK_LEVEL - QUANTITY_ON_HAND) as the parent's
    build-target proxy in the absence of a FORECAST_QTY / production-plan
    table. Revisit if/when Data Engineering exposes one.
  - decision_value here is a first-pass formula (exposure minus a rough
    cost-of-acting), not tuned against outcomes yet. Treat it as provisional
    until validated against real decision-value variance (§16).
"""

from agentic_restock.config import (
    TABLE_BOM,
    TABLE_DIM_PART,
    TABLE_DIM_SUPPLIER,
    TABLE_DIM_WAREHOUSE,
    TABLE_FACT_INVENTORY_SNAPSHOT,
    TABLE_FACT_INVENTORY_TRANSACTION,
    TABLE_FACT_RESTOCK_REQUEST,
    TABLE_SUPPLIER_CONTRACT,
    qualified_dim_table,
    qualified_fact_table,
    qualified_table,
)

TABLE_FACT_SUPPLIER_DELIVERY = "fact_supplier_delivery"
TABLE_FACT_SUPPLIER_QUALITY = "fact_supplier_quality"
TABLE_DIM_REQUEST_STATUS = "dim_request_status"

BOARD_TABLE_NAME = "inventory_signal_board"

# A part is "seasonally sampled" once transaction history covers at least
# this many trailing days; below that, adj_daily_burn falls back to the flat
# snapshot average rather than a noisy seasonal multiplier.
MIN_HISTORY_DAYS_FOR_SEASONALITY = 90


def build_signal_board_query(
    gold_catalog: str | None = None,
    dim_schema: str | None = None,
    facts_schema: str | None = None,
    app_catalog: str | None = None,
    app_schema: str | None = None,
) -> str:
    """Return the CREATE OR REPLACE TABLE statement for the signal board.

    Idempotent: re-running replaces the table wholesale. This is the fast
    layer conceptually (stock position, open commitments) fused with the
    slow layer (seasonality, supplier reliability, lead-time drift) into one
    table for phase 1; split the refresh cadence later if the nightly-cost
    of recomputing the slow columns hourly becomes a problem at production
    transaction volume.
    """
    snapshot = qualified_fact_table(TABLE_FACT_INVENTORY_SNAPSHOT, gold_catalog, facts_schema)
    txn = qualified_fact_table(TABLE_FACT_INVENTORY_TRANSACTION, gold_catalog, facts_schema)
    restock_request = qualified_fact_table(TABLE_FACT_RESTOCK_REQUEST, gold_catalog, facts_schema)
    part = qualified_dim_table(TABLE_DIM_PART, gold_catalog, dim_schema)
    warehouse = qualified_dim_table(TABLE_DIM_WAREHOUSE, gold_catalog, dim_schema)
    supplier = qualified_dim_table(TABLE_DIM_SUPPLIER, gold_catalog, dim_schema)
    request_status = qualified_dim_table(TABLE_DIM_REQUEST_STATUS, gold_catalog, dim_schema)

    bom = qualified_table(TABLE_BOM, app_catalog, app_schema)
    contract = qualified_table(TABLE_SUPPLIER_CONTRACT, app_catalog, app_schema)
    delivery = qualified_fact_table(TABLE_FACT_SUPPLIER_DELIVERY, gold_catalog, facts_schema)
    quality = qualified_fact_table(TABLE_FACT_SUPPLIER_QUALITY, gold_catalog, facts_schema)

    board = qualified_table(BOARD_TABLE_NAME, app_catalog, app_schema)

    return f"""
    CREATE OR REPLACE TABLE {board} AS

    -- Tiebreak on the surrogate key, not just the date: two rows can share a
    -- SNAPSHOT_DATE_KEY (e.g. a same-day correction inserted after the
    -- original), and ORDER BY date alone picks between them nondeterministically.
    WITH latest_snapshot AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY PART_KEY, WAREHOUSE_KEY
                ORDER BY SNAPSHOT_DATE_KEY DESC, INVENTORY_SNAPSHOT_KEY DESC
            ) AS rn
        FROM {snapshot}
    ),
    current_position AS (
        SELECT
            ls.PART_KEY, ls.WAREHOUSE_KEY,
            ls.QUANTITY_ON_HAND AS on_hand,
            ls.SAFETY_STOCK_QTY AS safety_stock,
            ls.MAX_STOCK_LEVEL AS max_stock,
            ls.AVG_DAILY_CONSUMPTION AS flat_daily_burn,
            ls.STOCK_VALUATION AS stock_valuation
        FROM latest_snapshot ls
        WHERE ls.rn = 1
    ),

    -- Nuance 4: seasonality-adjusted consumption. Compare each (part,
    -- warehouse)'s trailing-30-day ISSUE rate against its trailing-365-day
    -- rate; the ratio is the seasonal multiplier applied to the flat snapshot
    -- average. Grain matches the board's grain deliberately -- a part can
    -- ramp at the warehouse feeding one production line without ramping at
    -- another, and grouping by PART_KEY alone would blend those two demand
    -- patterns into one misleading multiplier. Falls back to the flat
    -- average when history is too thin to trust a multiplier (protects
    -- against day-one-of-a-new-part noise).
    consumption_windows AS (
        SELECT
            PART_KEY, WAREHOUSE_KEY,
            SUM(CASE WHEN TRANSACTION_TYPE = 'ISSUE'
                     AND TO_DATE(CAST(TRANSACTION_DATE_KEY AS STRING), 'yyyyMMdd')
                         >= DATE_SUB(CURRENT_DATE(), 30)
                THEN QUANTITY ELSE 0 END) / 30.0 AS recent_30d_rate,
            SUM(CASE WHEN TRANSACTION_TYPE = 'ISSUE'
                     AND TO_DATE(CAST(TRANSACTION_DATE_KEY AS STRING), 'yyyyMMdd')
                         >= DATE_SUB(CURRENT_DATE(), 365)
                THEN QUANTITY ELSE 0 END) / 365.0 AS trailing_365d_rate,
            DATEDIFF(
                CURRENT_DATE(),
                MIN(TO_DATE(CAST(TRANSACTION_DATE_KEY AS STRING), 'yyyyMMdd'))
            ) AS history_days
        FROM {txn}
        GROUP BY PART_KEY, WAREHOUSE_KEY
    ),
    seasonality AS (
        SELECT
            PART_KEY, WAREHOUSE_KEY,
            CASE
                WHEN history_days >= {MIN_HISTORY_DAYS_FOR_SEASONALITY}
                     AND trailing_365d_rate > 0
                THEN recent_30d_rate / trailing_365d_rate
                ELSE 1.0
            END AS seasonal_multiplier
        FROM consumption_windows
    ),

    -- Nuance 5 (supplier side): observed delay vs contracted lead time, and
    -- OTD rate feeding nuance 6, both from actual deliveries rather than a
    -- field nobody has updated since go-live.
    supplier_delivery_stats AS (
        SELECT
            SUPPLIER_KEY,
            AVG(DELAY_DAYS) AS observed_avg_delay_days,
            AVG(CASE WHEN OTD_FLAG THEN 1.0 ELSE 0.0 END) AS otd_rate,
            COUNT(*) AS delivery_count
        FROM {delivery}
        GROUP BY SUPPLIER_KEY
    ),
    supplier_quality_stats AS (
        SELECT
            SUPPLIER_KEY,
            AVG(PPM_LEVEL) AS avg_ppm,
            SUM(COST_OF_POOR_QUALITY) AS total_copq
        FROM {quality}
        GROUP BY SUPPLIER_KEY
    ),
    -- Nuance 6: composite reliability score, 0-100. OTD weighted higher than
    -- PPM because a late delivery blocks production immediately; a quality
    -- defect is usually caught before it stops a line.
    supplier_reliability AS (
        SELECT
            ds.SUPPLIER_KEY,
            ds.observed_avg_delay_days,
            ds.otd_rate,
            GREATEST(0, LEAST(100,
                (COALESCE(ds.otd_rate, 0.9) * 70)
                + (100 - LEAST(COALESCE(qs.avg_ppm, 0) / 100.0, 30)) * 0.3
            )) AS reliability_score
        FROM supplier_delivery_stats ds
        LEFT JOIN supplier_quality_stats qs ON ds.SUPPLIER_KEY = qs.SUPPLIER_KEY
    ),

    -- Preferred supplier per part, with contracted lead time and the
    -- observed-delay correction from actual deliveries.
    preferred_contract AS (
        SELECT
            c.part_id, c.supplier_id, c.lead_time_days AS contracted_lead_days,
            c.moq, c.pack_size, c.unit_cost AS contract_unit_cost,
            ROW_NUMBER() OVER (
                PARTITION BY c.part_id
                ORDER BY c.is_preferred DESC, c.lead_time_days ASC
            ) AS pref_rank
        FROM {contract} c
    ),
    part_lead_time AS (
        SELECT
            pc.part_id,
            pc.supplier_id AS preferred_supplier_id,
            pc.contracted_lead_days,
            sr.observed_avg_delay_days,
            pc.contracted_lead_days + COALESCE(sr.observed_avg_delay_days, 0) AS supplier_lead_days,
            sr.otd_rate,
            sr.reliability_score
        FROM preferred_contract pc
        LEFT JOIN {supplier} sup ON pc.supplier_id = sup.SUPPLIER_ID AND sup.IS_CURRENT = true
        LEFT JOIN supplier_reliability sr ON sup.SUPPLIER_KEY = sr.SUPPLIER_KEY
        WHERE pc.pref_rank = 1
    ),

    -- Nuance 1: network surplus. For each (part, warehouse) with a shortfall,
    -- find the best donor warehouse holding surplus above ITS OWN safety
    -- stock, and confirm the donor still clears its own cover after the
    -- move (donor protection — never propose a transfer that creates a new
    -- shortfall elsewhere).
    warehouse_position AS (
        SELECT PART_KEY, WAREHOUSE_KEY, on_hand, safety_stock,
               GREATEST(on_hand - safety_stock, 0) AS donor_surplus_qty
        FROM current_position
    ),
    best_donor AS (
        SELECT
            recipient.PART_KEY, recipient.WAREHOUSE_KEY,
            donor.WAREHOUSE_KEY AS donor_warehouse_key,
            donor.donor_surplus_qty,
            donor.safety_stock AS donor_safety_stock,
            donor.on_hand AS donor_on_hand,
            ROW_NUMBER() OVER (
                PARTITION BY recipient.PART_KEY, recipient.WAREHOUSE_KEY
                ORDER BY donor.donor_surplus_qty DESC
            ) AS donor_rank
        FROM warehouse_position recipient
        JOIN warehouse_position donor
            ON recipient.PART_KEY = donor.PART_KEY
           AND recipient.WAREHOUSE_KEY <> donor.WAREHOUSE_KEY
           AND donor.donor_surplus_qty > 0
        WHERE recipient.on_hand < recipient.safety_stock
    ),

    -- Nuance 2: BOM cascade value at risk. A component with healthy stock on
    -- its own (not already a shortfall) can still block a critical parent's
    -- build target. build_target uses (MAX_STOCK_LEVEL - on_hand) as the
    -- parent's demand proxy in the absence of a production-plan table — see
    -- module docstring.
    parent_build_target AS (
        SELECT PART_KEY, WAREHOUSE_KEY,
               GREATEST(max_stock - on_hand, 0) AS build_target_qty
        FROM current_position
    ),
    bom_cascade AS (
        SELECT
            comp_pos.PART_KEY AS component_part_key,
            comp_pos.WAREHOUSE_KEY AS warehouse_key,
            fg.PART_KEY AS parent_part_key,
            fg.PART_ID AS parent_part_id,
            fg.UNIT_COST AS parent_unit_cost,
            GREATEST(
                pbt.build_target_qty
                - FLOOR(comp_pos.on_hand / NULLIF(b.qty_per_unit, 0)),
                0
            ) AS parent_units_blocked,
            ROW_NUMBER() OVER (
                PARTITION BY comp_pos.PART_KEY, comp_pos.WAREHOUSE_KEY
                ORDER BY GREATEST(
                    pbt.build_target_qty
                    - FLOOR(comp_pos.on_hand / NULLIF(b.qty_per_unit, 0)), 0
                ) * fg.UNIT_COST DESC
            ) AS cascade_rank
        FROM current_position comp_pos
        JOIN {part} comp ON comp_pos.PART_KEY = comp.PART_KEY AND comp.IS_CURRENT = true
        JOIN {bom} b ON comp.PART_ID = b.component_part_id
        JOIN {part} fg ON b.fg_part_id = fg.PART_ID AND fg.IS_CURRENT = true
            -- dim_part carries two unnormalized spellings of the same class
            -- ('A-CRITICAL' and 'A - CRITICAL', ~50/50 split) -- a legacy
            -- merge artifact, same shape as the PART_ID/SUPPLIER_ID prefix
            -- split. An exact match here silently drops half of the true
            -- A-CRITICAL assemblies, so strip whitespace before comparing.
            AND REPLACE(fg.CRITICALITY_CLASS, ' ', '') = 'A-CRITICAL'
        JOIN parent_build_target pbt
            ON fg.PART_KEY = pbt.PART_KEY AND comp_pos.WAREHOUSE_KEY = pbt.WAREHOUSE_KEY
        WHERE comp_pos.on_hand >= comp_pos.safety_stock  -- not already caught by its own shortfall
    ),

    -- Suppression state: what's already an open commitment for this
    -- part/warehouse, and at what exposure it was raised, so
    -- rank_priority_actions can re-surface only on material change.
    -- FULFILLING is included alongside PENDING_APPROVAL/NEEDS_REVIEW/APPROVED
    -- so an approved line already being executed doesn't get a duplicate
    -- quote -- but rank_priority_actions also reads the two date columns
    -- below to detect a commitment that has sat too long (a PM decision
    -- overdue, or a fulfillment stalled past the part's own lead time) and
    -- re-surface it as STALLED_COMMITMENT rather than staying silently
    -- suppressed while its exposure keeps accruing.
    open_commitments AS (
        SELECT
            r.PART_KEY, r.WAREHOUSE_KEY,
            rs.REQUEST_STATUS AS commitment_state,
            r.REQUESTED_QTY AS open_commitment_qty,
            r.REQUESTED_DATE_KEY AS open_commitment_requested_date_key,
            r.DECISION_DATE_KEY AS open_commitment_decision_date_key,
            ROW_NUMBER() OVER (
                PARTITION BY r.PART_KEY, r.WAREHOUSE_KEY
                ORDER BY r.REQUESTED_DATE_KEY DESC
            ) AS recency_rank
        FROM {restock_request} r
        JOIN {request_status} rs ON r.REQUEST_STATUS_KEY = rs.REQUEST_STATUS_KEY
        WHERE rs.REQUEST_STATUS IN ('PENDING_APPROVAL', 'NEEDS_REVIEW', 'APPROVED', 'REJECTED', 'FULFILLING')
    )

    SELECT
        cp.PART_KEY AS part_key,
        cp.WAREHOUSE_KEY AS warehouse_key,
        p.PART_ID AS part_id,
        p.PART_NAME AS part_name,
        p.CRITICALITY_CLASS AS criticality_class,
        p.PART_TYPE AS part_type,
        p.UNIT_COST AS unit_cost,
        w.WAREHOUSE_ID AS warehouse_id,

        cp.on_hand,
        cp.safety_stock,
        cp.max_stock,
        cp.stock_valuation,

        -- Nuance 4
        cp.flat_daily_burn,
        COALESCE(s.seasonal_multiplier, 1.0) AS seasonal_multiplier,
        cp.flat_daily_burn * COALESCE(s.seasonal_multiplier, 1.0) AS adj_daily_burn,
        CASE WHEN cp.flat_daily_burn * COALESCE(s.seasonal_multiplier, 1.0) > 0
             THEN cp.on_hand / (cp.flat_daily_burn * COALESCE(s.seasonal_multiplier, 1.0))
             ELSE NULL END AS days_of_cover,

        -- Nuance 5 + 6
        plt.preferred_supplier_id,
        plt.contracted_lead_days,
        plt.observed_avg_delay_days,
        plt.supplier_lead_days AS effective_lead_days,
        plt.otd_rate,
        plt.reliability_score,

        -- Nuance 1
        bd.donor_warehouse_key,
        dw.WAREHOUSE_ID AS best_donor_warehouse_id,
        bd.donor_surplus_qty AS network_surplus_qty,
        bd.donor_safety_stock,
        (bd.donor_on_hand - LEAST(bd.donor_surplus_qty, GREATEST(cp.safety_stock - cp.on_hand, 0)))
            - bd.donor_safety_stock AS donor_cover_after_units,

        -- Nuance 2
        bc.parent_part_id AS threatened_parent_part_id,
        bc.parent_units_blocked,
        bc.parent_units_blocked * bc.parent_unit_cost AS value_at_risk,

        -- Suppression state (nuance 8, applied in rank_priority_actions)
        oc.commitment_state,
        oc.open_commitment_qty,
        oc.open_commitment_requested_date_key,
        oc.open_commitment_decision_date_key,

        CURRENT_TIMESTAMP() AS board_refreshed_at

    FROM current_position cp
    JOIN {part} p ON cp.PART_KEY = p.PART_KEY AND p.IS_CURRENT = true
    JOIN {warehouse} w ON cp.WAREHOUSE_KEY = w.WAREHOUSE_KEY
    LEFT JOIN seasonality s ON cp.PART_KEY = s.PART_KEY AND cp.WAREHOUSE_KEY = s.WAREHOUSE_KEY
    LEFT JOIN part_lead_time plt ON p.PART_ID = plt.part_id
    LEFT JOIN best_donor bd
        ON cp.PART_KEY = bd.PART_KEY AND cp.WAREHOUSE_KEY = bd.WAREHOUSE_KEY AND bd.donor_rank = 1
    LEFT JOIN {warehouse} dw ON bd.donor_warehouse_key = dw.WAREHOUSE_KEY
    LEFT JOIN bom_cascade bc
        ON cp.PART_KEY = bc.component_part_key AND cp.WAREHOUSE_KEY = bc.warehouse_key AND bc.cascade_rank = 1
    LEFT JOIN open_commitments oc
        ON cp.PART_KEY = oc.PART_KEY AND cp.WAREHOUSE_KEY = oc.WAREHOUSE_KEY AND oc.recency_rank = 1
    WHERE p.LIFECYCLE_STATUS = 'ACTIVE'
      AND w.OPERATIONAL_STATUS = 'ACTIVE'
    """.strip()
