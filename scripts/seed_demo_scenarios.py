"""Centralized demo data feeder: seeds intelligence-scenario data and reports
layer readiness.

dim_bom and dim_supplier_contract are "ours" (see CLAUDE.md's Data layer
section) and were bootstrapped by notebooks/schema_bootstrap.ipynb with a
handful of placeholder rows. Re-running that notebook is destructive -- its
single job task also CREATE OR REPLACEs quote_metadata with 3 hardcoded rows,
which would wipe live approval history -- so this script grows dim_bom and
dim_supplier_contract in place instead, following the same idempotent,
pre-check-before-write pattern as add_restock_note_column.py.

fact_supplier_delivery is different: it is Data Engineering's read-only fact
table (see config.py's ownership split), and the only OTHER sanctioned way to
add to it is notebooks/simulation/generate_sim_data.py's scenario engine
(which plants LEAD_DRIFT/LATE_PO scenarios and records them as ground truth
in sim_pair_scenarios, so a detection claim stays attributable -- see
docs/market_evidence_phase1.md). Rows this script inserts here are NOT
attributed that way -- there is no matching sim_pair_scenarios entry -- a
deliberate, explicit trade-off accepted for demo purposes, not an oversight.
Rows are tagged DW_SOURCE = 'DEMO_SEED' and DELIVERY_ID prefixed
'DEMO-LEADTIME-' so they stay identifiable as seeded rather than real
deliveries if anyone audits this table later.

All rows below are derived from the live gold_dev.supply_chain_analytics
board data (real part IDs, real on-hand/build-target quantities, real
supplier IDs matched by SUPPLIER_TYPE to parts they plausibly supply), not
invented -- see docs/market_evidence_phase1.md and the demo-readiness review
that produced them. Deliberately moderate volume -- this is a small, curated
set of scenarios, not a bulk generator (that's generate_sim_data.py's job).

Idempotent: skips any (fg_part_id, component_part_id) pair, (part_id,
supplier_id) pair, or DELIVERY_ID that already exists, safe to re-run. Run
again after adding a new scenario row above -- only the new rows get inserted.

--rebuild-facts (DESTRUCTIVE, separate from the default idempotent seed):
notebooks/simulation/generate_sim_data.py and its job have been retired --
its dependency (src/agentic_restock/simulation.py) went missing from the
repo, and its opaque ~68K/63K-row random simulation was replaced by this
fully hand-authored, curated dataset instead, by explicit choice: every row
here has a known reason to exist. This clears and rewrites
fact_inventory_snapshot, fact_inventory_transaction, fact_procurement,
fact_plant_capacity, sim_events, and sim_pair_scenarios (DELETE, not DROP --
the table DDL is Data Engineering's, only the rows are ours to replace in
dev). It preserves every (part, warehouse) combination the rest of this
script's scenarios (BOM cascades, lead-time drift, existing restock
requests, the live rank_priority_actions_diverse candidates) depend on,
verified against live data before this was written -- see the demo-rebuild
review that produced it. fact_plant_capacity, sim_events, and
sim_pair_scenarios are cleared but deliberately NOT reseeded:
fact_plant_capacity is unused by any current intelligence function (its
PLANT_ID values don't even match dim_plant's real IDs -- it was always
disconnected placeholder data), and sim_events/sim_pair_scenarios existed to
give the old random simulator's output backtest attribution, which no
longer applies to a hand-authored dataset with no randomness to attribute.

Usage:
    PYTHONPATH=src python3 scripts/seed_demo_scenarios.py --profile anurag-r
    PYTHONPATH=src python3 scripts/seed_demo_scenarios.py --report --profile anurag-r
    PYTHONPATH=src python3 scripts/seed_demo_scenarios.py --rebuild-facts --profile anurag-r
"""

import argparse
import datetime
import re
import sys

sys.path.insert(0, "src")

from databricks.sdk import WorkspaceClient

from agentic_restock.config import (
    TABLE_BOM,
    TABLE_SUPPLIER_CONTRACT,
    qualified_dim_table,
    qualified_fact_table,
    qualified_table,
)

WAREHOUSE_ID = "d2533a75c1bd9265"

# WAREHOUSE_KEY on the seeded fact_supplier_delivery rows -- WH001, an
# arbitrary valid warehouse. scan_leadtime_drift groups purely by
# SUPPLIER_KEY, so which warehouse these are attached to doesn't affect
# the nuance; it just has to be a real key to satisfy the fact table.
WAREHOUSE_KEY_FOR_DEMO_DELIVERIES = 1

# (fg_part_id, component_part_id, qty_per_unit) -- a component with healthy
# stock on its own that still blocks an A-CRITICAL parent's build target.
# P1002/P1015 is the flagship: an oxygen sensor sitting well above its own
# safety stock, yet 125 units short of what the 1.2L engine's build target
# needs -- exactly the risk a per-part threshold check can't see.
BOM_ROWS = [
    ("P1002", "P1015", 1),
    ("P1006", "PRT-022", 3),
    ("P1008", "PRT-038", 1),
    ("PRT-031", "P1016", 2),
]

# (part_id, supplier_id, lead_time_days, moq, pack_size, unit_cost, is_preferred)
# supplier_id chosen from suppliers that already have fact_supplier_delivery /
# fact_supplier_quality history, so otd_rate/reliability_score populate
# immediately. P1006 -> SUP005 is deliberate: SUP005 (Aisin, Transmission &
# Drivetrain) has a 0.12 OTD rate and 9-day average delay in the existing
# delivery facts -- a real problem supplier on a critical assembly.
CONTRACT_ROWS = [
    ("P1001", "SUP001", 45, 20, 1, 165000.00, True),
    ("P1002", "SUP-031", 40, 20, 1, 125000.00, True),
    ("P1003", "SUP002", 12, 200, 20, 3200.00, True),
    ("P1004", "SUP004", 15, 100, 4, 9500.00, True),
    ("P1005", "SUP-039", 7, 100, 10, 6800.00, True),
    ("P1006", "SUP005", 60, 10, 1, 88000.00, True),
    ("P1008", "SUP007", 25, 30, 1, 14200.00, True),
    ("P1010", "SUP010", 20, 50, 2, 7600.00, True),
    ("P1011", "SUP-023", 22, 50, 1, 8400.00, True),
    ("P1012", "SUP-029", 18, 80, 4, 12800.00, True),
    ("P1013", "SUP-024", 14, 60, 2, 5200.00, True),
    ("P1014", "SUP-030", 8, 300, 20, 1800.00, True),
    ("P1015", "SUP-040", 9, 150, 10, 3400.00, True),
    ("P1016", "SUP-030", 10, 120, 6, 4600.00, True),
    ("P1017", "SUP-022", 11, 100, 5, 7200.00, True),
    ("P1018", "SUP-028", 28, 20, 1, 22000.00, True),
    ("P1019", "SUP-026", 16, 60, 2, 9800.00, True),
    ("P1020", "SUP-029", 13, 150, 6, 6400.00, True),
    ("PRT-021", "SUP-024", 17, 40, 1, 2450.00, True),
    ("PRT-022", "SUP-033", 9, 200, 20, 890.50, True),
    ("PRT-023", "SUP008", 20, 50, 2, 4200.00, True),
    ("PRT-025", "SUP-026", 15, 60, 2, 5600.00, True),
    ("PRT-026", "SUP-030", 7, 300, 25, 320.00, True),
    ("PRT-027", "SUP-031", 35, 30, 1, 18500.00, True),
    ("PRT-028", "SUP-028", 19, 40, 2, 1850.00, True),
    ("PRT-029", "SUP-029", 10, 400, 50, 1200.00, True),
    ("PRT-030", "SUP-038", 12, 80, 4, 3800.00, True),
    ("PRT-032", "SUP-031", 18, 40, 2, 4500.00, True),
    ("PRT-033", "SUP007", 24, 30, 1, 6800.00, True),
    ("PRT-035", "SUP-023", 15, 80, 4, 2800.00, True),
    ("PRT-036", "SUP-035", 9, 150, 10, 950.00, True),
    ("PRT-038", "SUP-037", 12, 100, 5, 8500.00, True),
]

# (delivery_id, supplier_id, planned_date_key, delivery_date_key, quantity) --
# real, chronic lateness on a supplier that already has a preferred contract
# for a part in the demo (see CONTRACT_ROWS above), so scan_leadtime_drift
# has more than the one existing SUP005/Aisin example to show. PRT-033 also
# already carries the flagship BOM cascade (blocks via PRT-037) -- one part,
# two independently-detected problems. P1010 already has a live
# NEEDS_REVIEW restock request in the demo data -- this ties a supplier
# reliability story to a part already under PM review.
SUPPLIER_DELIVERY_ROWS = [
    # PRT-033 (Steering Rack Assembly) <- SUP007 (Sona Comstar Steering Systems)
    ("DEMO-LEADTIME-SUP007-01", "SUP007", 20260805, 20260812, 120),
    ("DEMO-LEADTIME-SUP007-02", "SUP007", 20260810, 20260816, 150),
    ("DEMO-LEADTIME-SUP007-03", "SUP007", 20260815, 20260823, 100),
    ("DEMO-LEADTIME-SUP007-04", "SUP007", 20260820, 20260826, 130),
    ("DEMO-LEADTIME-SUP007-05", "SUP007", 20260825, 20260830, 110),
    # P1010 (Front MacPherson Strut Suspension) <- SUP010 (Gabriel India Suspension Systems)
    ("DEMO-LEADTIME-SUP010-01", "SUP010", 20260803, 20260809, 200),
    ("DEMO-LEADTIME-SUP010-02", "SUP010", 20260808, 20260815, 180),
    ("DEMO-LEADTIME-SUP010-03", "SUP010", 20260813, 20260818, 220),
    ("DEMO-LEADTIME-SUP010-04", "SUP010", 20260818, 20260824, 190),
    ("DEMO-LEADTIME-SUP010-05", "SUP010", 20260823, 20260828, 210),
    # P1015 <- SUP-040 and PRT-027 <- SUP-031. These two suppliers serve the
    # parts that actually WIN their signal type's ranking slot, and both had
    # exactly one on-time delivery on record -- so nuance 5 was provably
    # working (SUP007/SUP010 above) while being invisible in every live run,
    # because the drill-down only ever looks at the winning candidate. Delays
    # are kept moderate (3-4 days average, not the 5-9 above) so the chosen
    # purchase still reads as the right call, just with a lead time the
    # contract understates.
    ("DEMO-LEADTIME-SUP040-01", "SUP-040", 20260806, 20260806, 150),
    ("DEMO-LEADTIME-SUP040-02", "SUP-040", 20260812, 20260816, 150),
    ("DEMO-LEADTIME-SUP040-03", "SUP-040", 20260818, 20260823, 160),
    ("DEMO-LEADTIME-SUP040-04", "SUP-040", 20260824, 20260829, 150),
    ("DEMO-LEADTIME-SUP040-05", "SUP-040", 20260828, 20260902, 155),
    ("DEMO-LEADTIME-SUP031-01", "SUP-031", 20260804, 20260804, 900),
    ("DEMO-LEADTIME-SUP031-02", "SUP-031", 20260810, 20260815, 850),
    ("DEMO-LEADTIME-SUP031-03", "SUP-031", 20260816, 20260822, 950),
    ("DEMO-LEADTIME-SUP031-04", "SUP-031", 20260822, 20260827, 900),
    ("DEMO-LEADTIME-SUP031-05", "SUP-031", 20260827, 20260902, 880),
]

# ══════════════════════════════════════════════════════════════════════════
# --rebuild-facts dataset (see the module docstring's DESTRUCTIVE note).
# ══════════════════════════════════════════════════════════════════════════

# (part_id, warehouse_id, on_hand, safety_stock, max_stock, flat_daily_burn,
# unit_cost, reason) -- the exact combinations the BOM cascades above, the
# lead-time-drift contracts above, and the existing fact_restock_request
# lines depend on. Values captured from the live board before this rebuild,
# so every scenario already verified working this session keeps working
# after it. unit_cost is repeated here (also on dim_part) only so this
# script doesn't need a live dim_part round-trip to fill STOCK_VALUATION.
STORY_SNAPSHOT_ROWS = [
    ("P1001", "WH-035", 367, 505, 1318, 16.67, 165000.00, "existing FULFILLING restock line"),
    ("P1002", "WH-026", 368, 135, 621, 13.50, 125000.00, "BOM cascade parent (blocks P1015)"),
    ("P1002", "WH004", 89, 273, 876, 12.50, 125000.00, "existing PENDING_APPROVAL / STALLED_COMMITMENT line"),
    ("P1004", "WH007", 232, 65, 333, 3.77, 9500.00, "existing COMPLETED restock line"),
    ("P1006", "WH005", 261, 67, 456, 6.47, 88000.00, "BOM cascade parent (blocks PRT-022)"),
    ("P1008", "WH014", 512, 83, 837, 0.00, 14200.00, "BOM cascade parent (blocks PRT-038)"),
    ("P1010", "WH-026", 145, 174, 778, 16.87, 7600.00, "existing NEEDS_REVIEW restock line"),
    ("P1015", "WH-026", 128, 31, 188, 3.00, 3400.00, "BOM cascade component (flagship, blocks P1002)"),
    ("P1016", "WH007", 353, 36, 733, 25.00, 4600.00, "BOM cascade component (blocks PRT-031)"),
    ("P1018", "WH018", 101, 424, 1043, 11.37, 22000.00, "existing PENDING_APPROVAL restock line"),
    ("PRT-022", "WH005", 520, 140, 600, 3.00, 890.50, "BOM cascade component (blocks P1006)"),
    ("PRT-027", "WH-037", 391, 658, 2262, 34.43, 18500.00, "live STOCK_THRESHOLD diverse candidate"),
    ("PRT-031", "WH007", 92, 61, 298, 5.27, 12000.00, "BOM cascade parent (blocks P1016)"),
    ("PRT-033", "WH002", 10, 15, 60, 1.50, 6800.00, "BOM cascade parent (blocks PRT-037) + lead-time drift"),
    ("PRT-037", "WH002", 60, 45, 250, 5.00, 1450.00, "BOM cascade component (original pair, blocks PRT-033)"),
    ("PRT-038", "WH014", 320, 50, 357, 18.00, 8500.00, "BOM cascade component (blocks P1008)"),
    ("P1003", "WH003", 45, 180, 600, 6.00, 3200.00, "validate_genie_groundedness.py's 3 golden test scenarios"),
]

# (part_id, warehouse_id, on_hand, safety_stock, max_stock, flat_daily_burn,
# unit_cost) x2 per part -- a real shortfall at one warehouse with real,
# donor-protected surplus at another, so scan_transfer_options (nuance 1)
# has more than the story rows' incidental transfers to show. 3 fresh parts,
# not otherwise used above.
TRANSFER_SNAPSHOT_ROWS = [
    ("P1017", "WH010", 40, 120, 400, 8.00, 7200.00),   # shortfall
    ("P1017", "WH020", 350, 100, 450, 8.00, 7200.00),  # donor surplus
    ("PRT-024", "WH011", 30, 90, 300, 6.00, 3150.00),  # shortfall
    ("PRT-024", "WH-021", 280, 70, 320, 6.00, 3150.00),  # donor surplus
    ("P1013", "WH012", 25, 80, 280, 5.00, 5200.00),    # shortfall
    ("P1013", "WH-022", 260, 60, 300, 5.00, 5200.00),   # donor surplus
]

# (part_id, warehouse_id, on_hand, safety_stock, max_stock, avg_daily_consumption,
# unit_cost) -- healthy on their own; scan_demand_shift (nuance 4) flags them
# from the transaction history below, not from stock position.
DEMAND_SHIFT_SNAPSHOT_ROWS = [
    ("P1019", "WH-023", 350, 100, 500, 10.00, 9800.00),   # SPIKE
    ("PRT-030", "WH-024", 200, 70, 300, 5.56, 3800.00),   # DROP
    ("P1020", "WH-025", 300, 90, 450, 5.00, 6400.00),     # SPIKE
]

# (part_id, warehouse_id, transaction_date_key, quantity) -- TRANSACTION_TYPE
# is always ISSUE. history_days must clear MIN_HISTORY_DAYS_FOR_SEASONALITY
# (90, see signal_board.py) so every date below is planted well before that;
# the recent_30d vs trailing_365d split is arithmetic, not per-day realism --
# see the demo-rebuild review for the worked math (target multiplier ~1.5 for
# a SPIKE, ~0.6 for a DROP). Dated relative to "today" 2026-09-02.
DEMAND_SHIFT_TRANSACTION_ROWS = [
    # P1019 -- SPIKE. Old baseline 3200 spread across ~170 days (outside the
    # last 30), recent 450 inside the last 30 -- multiplier ~1.5.
    ("P1019", "WH-023", 20260315, 500),
    ("P1019", "WH-023", 20260401, 450),
    ("P1019", "WH-023", 20260415, 480),
    ("P1019", "WH-023", 20260501, 460),
    ("P1019", "WH-023", 20260601, 440),
    ("P1019", "WH-023", 20260701, 420),
    ("P1019", "WH-023", 20260801, 450),
    ("P1019", "WH-023", 20260810, 150),
    ("P1019", "WH-023", 20260820, 150),
    ("P1019", "WH-023", 20260830, 150),
    # PRT-030 -- DROP. Old baseline 1930, recent 100 -- multiplier ~0.6.
    ("PRT-030", "WH-024", 20260320, 300),
    ("PRT-030", "WH-024", 20260420, 320),
    ("PRT-030", "WH-024", 20260520, 310),
    ("PRT-030", "WH-024", 20260620, 330),
    ("PRT-030", "WH-024", 20260720, 340),
    ("PRT-030", "WH-024", 20260801, 330),
    ("PRT-030", "WH-024", 20260815, 50),
    ("PRT-030", "WH-024", 20260828, 50),
    # P1020 -- SPIKE. Old baseline 1600, recent 225 -- multiplier ~1.5.
    ("P1020", "WH-025", 20260310, 250),
    ("P1020", "WH-025", 20260410, 260),
    ("P1020", "WH-025", 20260510, 270),
    ("P1020", "WH-025", 20260610, 260),
    ("P1020", "WH-025", 20260710, 280),
    ("P1020", "WH-025", 20260801, 280),
    ("P1020", "WH-025", 20260812, 110),
    ("P1020", "WH-025", 20260825, 115),
    # P1015 @ WH-026 and PRT-027 @ WH-037 -- SPIKE, multiplier ~1.5. Same
    # reason as the SUP-040/SUP-031 deliveries above: these are the parts that
    # win their signal type, so this is the only way nuance 4 reaches a live
    # report. Volumes track each part's own flat_daily_burn (3.0/day and
    # 34.43/day) so the trailing-365d rate stays consistent with the snapshot
    # average the board already carries -- the multiplier is the story, not a
    # contradiction between the two.
    ("P1015", "WH-026", 20260305, 90),
    ("P1015", "WH-026", 20260320, 95),
    ("P1015", "WH-026", 20260405, 90),
    ("P1015", "WH-026", 20260420, 95),
    ("P1015", "WH-026", 20260505, 90),
    ("P1015", "WH-026", 20260520, 95),
    ("P1015", "WH-026", 20260605, 90),
    ("P1015", "WH-026", 20260620, 95),
    ("P1015", "WH-026", 20260705, 90),
    ("P1015", "WH-026", 20260720, 95),
    ("P1015", "WH-026", 20260801, 40),
    ("P1015", "WH-026", 20260810, 45),
    ("P1015", "WH-026", 20260820, 45),
    ("P1015", "WH-026", 20260830, 45),
    ("PRT-027", "WH-037", 20260310, 1000),
    ("PRT-027", "WH-037", 20260325, 1000),
    ("PRT-027", "WH-037", 20260410, 1000),
    ("PRT-027", "WH-037", 20260425, 1000),
    ("PRT-027", "WH-037", 20260510, 1000),
    ("PRT-027", "WH-037", 20260525, 1000),
    ("PRT-027", "WH-037", 20260610, 1000),
    ("PRT-027", "WH-037", 20260625, 1000),
    ("PRT-027", "WH-037", 20260710, 1000),
    ("PRT-027", "WH-037", 20260725, 1000),
    ("PRT-027", "WH-037", 20260801, 1050),
    ("PRT-027", "WH-037", 20260808, 520),
    ("PRT-027", "WH-037", 20260818, 517),
    ("PRT-027", "WH-037", 20260828, 517),
]

# (part_id, supplier_id, order_date_key, expected_date_key, order_qty,
# unit_rate, status) -- fulfillment_guardrail's "open PO already covers the
# gap" check (fact_procurement, STATUS IN ('ISSUED','PARTIAL')) needs at
# least one row to have anything to find. Ties to the existing P1002@WH004
# PENDING_APPROVAL line -- an 800-unit PO already in flight for the same
# part, a real NEEDS_REVIEW scenario if the Supervisor checks before that
# line is approved.
PROCUREMENT_ROWS = [
    ("P1002", "SUP-031", 20260828, 20260930, 800, 125000.00, "ISSUED"),
]

PLANT_KEY_FOR_DEMO_PROCUREMENT = 1

# ── Quote pruning (--prune-quotes) ───────────────────────────────────────
# Repeated dev runs leave a pile of superseded quotes in the PM's approval
# queue: each one shows on the pending-quotes page, and each one suppresses its
# parts for 2 days (nuance 8), so the next run is pushed onto progressively
# weaker candidates. --prune-quotes is the demo reset.
#
# These four are preserved unconditionally. Each is a single
# fact_restock_request line that exists to give a review-app page or a signal
# type something real to show -- none is a report anyone reads, and deleting
# one empties a page.
PRESERVED_QUOTE_IDS = {
    "QT-20260901-7B7C27": "P1001@WH-035 FULFILLING -- the only row FulfillingOrdersPage has",
    "QT-20260901-C22E38": "P1004@WH007 COMPLETED -- the completed-line example",
}

# Deliberately NOT preserved: an aged PENDING_APPROVAL line. One used to live
# here (P1002@WH004, requested 20260828) purely to keep STALLED_COMMITMENT a
# live signal type, and it was dropped because an aged pending line also sits
# on the PM's approval page looking like real work nobody has done.
#
# Worth knowing when that signal type next goes quiet: the board reads only the
# MOST RECENT open commitment per (part, warehouse) (recency_rank = 1, see
# signal_board.py's open_commitments CTE). So any run that raises a fresh quote
# on a part masks that part's older, already-stale line, and STALLED_COMMITMENT
# disappears until the new line itself ages past the 2-day PM-turnaround
# threshold. It is self-restoring, not broken -- the signal type is live exactly
# when some open commitment has genuinely been sitting too long, which is the
# point of it.


def prune_quotes(w: WorkspaceClient, keep_recent: int = 1) -> None:
    """DESTRUCTIVE: drop every quote except PRESERVED_QUOTE_IDS and the
    `keep_recent` most recent quotes that actually have part-lines.

    "That actually have part-lines" is the important qualifier: a quote_metadata
    header with no fact_restock_request rows renders an empty approval screen,
    so it is never what you want to keep as the demo quote.
    """
    quote_table = qualified_table("quote_metadata")
    request_table = qualified_fact_table("fact_restock_request")

    rows = run_query(
        w,
        f"""SELECT q.quote_id, CAST(q.created_at AS STRING),
                   (SELECT COUNT(*) FROM {request_table} r WHERE r.QUOTE_ID = q.quote_id)
            FROM {quote_table} q ORDER BY q.created_at DESC""",
    )
    if not rows:
        print("quote_metadata is empty -- nothing to prune.")
        return

    with_lines = [r[0] for r in rows if int(r[2]) > 0 and r[0] not in PRESERVED_QUOTE_IDS]
    keep = set(PRESERVED_QUOTE_IDS) | set(with_lines[:keep_recent])
    drop = [r for r in rows if r[0] not in keep]

    print(f"Pruning quotes (keeping {len(keep)}, dropping {len(drop)}):\n")
    for quote_id, created_at, lines in rows:
        if quote_id in keep:
            why = PRESERVED_QUOTE_IDS.get(quote_id, "most recent quote with part-lines")
            print(f"  KEEP  {quote_id:22} {lines} line(s)  {created_at[:19]}  {why.split(' -- ')[0]}")
    for quote_id, created_at, lines in drop:
        note = "no part-lines" if int(lines) == 0 else f"{lines} line(s)"
        print(f"  DROP  {quote_id:22} {note:13} {created_at[:19]}")

    if not drop:
        print("\nNothing to drop.")
        return

    id_list = ", ".join(sql_str(r[0]) for r in drop)
    # Lines first: a header with orphaned lines is worse than an orphaned header.
    for table in (request_table, quote_table):
        column = "QUOTE_ID" if table == request_table else "quote_id"
        w.statement_execution.execute_statement(
            statement=f"DELETE FROM {table} WHERE {column} IN ({id_list})",
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
        )
    print(f"\nDeleted {len(drop)} quote(s) from quote_metadata and fact_restock_request.")


# ── Coverage generators ──────────────────────────────────────────────────
# The hand-authored rows above give each nuance a named, explainable example.
# These two fill the gap that example alone leaves: a nuance the ranking's
# actual winner has no data for is a nuance that never reaches a live report.
# The first multi-candidate run hit exactly that -- 5 of 44 (part, warehouse)
# pairs had any transaction history and 4 of 33 contracted suppliers had any
# delivery record, so scan_demand_shift and scan_leadtime_drift were blind for
# whichever part the ranking surfaced. Both generators are deterministic
# (assignment fixed by sorted position, never random) so a rebuild reproduces
# the same dataset, and both derive volumes from numbers already on the row
# rather than inventing unrelated ones.

# Baseline dates sit outside the trailing-30-day window and start ~180 days
# back, so history_days clears MIN_HISTORY_DAYS_FOR_SEASONALITY (90, see
# signal_board.py); the three recent dates sit inside it.
_BASELINE_TXN_DATE_KEYS = [
    20260305, 20260320, 20260405, 20260420, 20260505,
    20260520, 20260605, 20260620, 20260705, 20260801,
]
_RECENT_TXN_DATE_KEYS = [20260810, 20260820, 20260830]

# spike / drop / flat, assigned round-robin by sorted (part, warehouse). Two
# thirds land outside scan_demand_shift's 0.8-1.2 band, so a ranked candidate
# usually has a real seasonal correction to report and sometimes honestly has
# none -- rigging every part to drift would make the signal meaningless.
_SEASONAL_PATTERN = (1.50, 0.65, 1.00)


def _split_qty(total: int, buckets: int) -> list[int]:
    """Whole units across N dates, remainder on the first, sum exact."""
    base = total // buckets
    out = [base] * buckets
    out[0] += total - base * buckets
    return out


def _generate_consumption_history(snapshot_rows, already_seeded) -> list[tuple]:
    """ISSUE rows for every (part, warehouse) that has a real burn rate.

    Volumes come from the pair's own AVG_DAILY_CONSUMPTION: the trailing-365d
    total is burn x 365, so the seasonal multiplier the board computes is the
    only thing that disagrees with the flat snapshot average -- which is the
    signal, rather than a contradiction between the two numbers.
    """
    rows: list[tuple] = []
    pairs = sorted({(r[0], r[1], r[5]) for r in snapshot_rows})
    for idx, (part_id, warehouse_id, burn) in enumerate(pairs):
        if (part_id, warehouse_id) in already_seeded or not burn:
            continue
        mult = _SEASONAL_PATTERN[idx % len(_SEASONAL_PATTERN)]
        # float() first: a background row's burn arrives as a Decimal from
        # the live dim_part lookup, and round(Decimal) stays a Decimal.
        recent_total = round(mult * float(burn) * 30)
        baseline_total = round(float(burn) * 365) - recent_total
        if baseline_total <= 0 or recent_total <= 0:
            continue
        for key, qty in zip(_BASELINE_TXN_DATE_KEYS, _split_qty(baseline_total, len(_BASELINE_TXN_DATE_KEYS))):
            if qty > 0:
                rows.append((part_id, warehouse_id, key, qty))
        for key, qty in zip(_RECENT_TXN_DATE_KEYS, _split_qty(recent_total, len(_RECENT_TXN_DATE_KEYS))):
            if qty > 0:
                rows.append((part_id, warehouse_id, key, qty))
    return rows


# Six deliveries per generated supplier, on these planned dates. A drifting
# supplier slips 5-6 days on five of six; a reliable one delivers on contract
# with one 1-day slip. Both patterns are sized to survive blending with the
# one or two rows a thin supplier already has on file (all of those are
# within a day of plan), so a drifting supplier still clears
# scan_leadtime_drift's 3-day floor and a reliable one still reads as on
# contract. Every other supplier by sorted id drifts, so roughly half the
# working set carries a real lead-time correction and the other half
# genuinely does not -- a dataset where every supplier is late makes the
# signal meaningless.
_DELIVERY_PLANNED_DATE_KEYS = [20260804, 20260809, 20260814, 20260819, 20260824, 20260829]
_DRIFTING_DELAYS = (0, 5, 5, 6, 5, 6)
_RELIABLE_DELAYS = (0, 0, 0, 1, 0, 0)


def _generate_supplier_delivery_rows(w: WorkspaceClient) -> list[tuple]:
    """Deliveries for contracted suppliers that have no delivery record at all.

    Deliberately only those: a supplier that already has rows (DE's own, or
    the hand-authored SUP007/SUP010/SUP-040/SUP-031 sets above) keeps its real
    observed average untouched -- this fills the hole rather than moving
    numbers that already mean something.
    """
    contracted = sorted({r[1] for r in CONTRACT_ROWS})
    # 3 is the cut-off, not 1: nearly every supplier here carries a single
    # on-time delivery, which averages to a 0.0-day drift and reads as "on
    # contract" while actually meaning "no record". That single row is what
    # made nuance 5 silently unavailable for most of the working set.
    with_history = {
        row[0]
        for row in run_query(
            w,
            f"SELECT s.SUPPLIER_ID FROM {qualified_fact_table('fact_supplier_delivery')} d "
            f"JOIN {qualified_dim_table('dim_supplier')} s ON s.SUPPLIER_KEY = d.SUPPLIER_KEY "
            f"WHERE s.IS_CURRENT = true GROUP BY s.SUPPLIER_ID HAVING COUNT(*) >= 3",
        )
    }
    hand_authored = {r[1] for r in SUPPLIER_DELIVERY_ROWS}
    moq_by_supplier = {r[1]: r[3] for r in CONTRACT_ROWS}
    rows: list[tuple] = []
    for idx, supplier_id in enumerate(s for s in contracted if s not in with_history and s not in hand_authored):
        delays = _DRIFTING_DELAYS if idx % 2 == 0 else _RELIABLE_DELAYS
        qty = max(int(moq_by_supplier.get(supplier_id, 50)), 10)
        for n, (planned_key, delay) in enumerate(zip(_DELIVERY_PLANNED_DATE_KEYS, delays), start=1):
            rows.append((
                f"DEMO-DELIV-{supplier_id}-{n:02d}",
                supplier_id,
                planned_key,
                _shift_date_key(planned_key, delay),
                qty,
            ))
    return rows


# Every (part_id, warehouse_id) touched by the rebuilt dataset, in one place
# so the background-row generator below can skip them cleanly.
_NAMED_SNAPSHOT_ROWS = STORY_SNAPSHOT_ROWS + TRANSFER_SNAPSHOT_ROWS + DEMAND_SHIFT_SNAPSHOT_ROWS

# Global (no required part filter) phase-1 scan functions checked by
# --report -- evaluate_suppliers/evaluate_feasibility are per-part lookups,
# not scanners, so they're not meaningful "is there any candidate at all"
# checks and are excluded here.
REPORT_SCAN_FUNCTIONS = [
    ("scan_transfer_options (nuance 1: network surplus)", "scan_transfer_options()"),
    ("scan_assembly_risk (nuance 2: BOM cascade)", "scan_assembly_risk()"),
    ("scan_demand_shift (nuance 4: seasonality)", "scan_demand_shift(NULL)"),
    ("scan_leadtime_drift (nuance 5: supplier drift)", "scan_leadtime_drift()"),
]

ALL_SIGNAL_TYPES = ["STOCK_THRESHOLD", "BOM_CASCADE_RISK", "STALLED_COMMITMENT"]


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _date_from_key(date_key: int) -> datetime.date:
    s = str(date_key)
    return datetime.date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _date_key_diff(planned_date_key: int, delivery_date_key: int) -> int:
    return (_date_from_key(delivery_date_key) - _date_from_key(planned_date_key)).days


def _shift_date_key(date_key: int, days: int) -> int:
    shifted = _date_from_key(date_key) + datetime.timedelta(days=days)
    return int(shifted.strftime("%Y%m%d"))


def run_query(w: WorkspaceClient, statement: str) -> list[list]:
    result = w.statement_execution.execute_statement(
        statement=statement, warehouse_id=WAREHOUSE_ID, wait_timeout="30s"
    )
    return (result.result.data_array or []) if result.result else []


def existing_pairs(w: WorkspaceClient, table: str, key_cols: tuple[str, str]) -> set[tuple[str, str]]:
    col_a, col_b = key_cols
    return {(r[0], r[1]) for r in run_query(w, f"SELECT {col_a}, {col_b} FROM {table}")}


def seed(w: WorkspaceClient) -> None:
    bom_table = qualified_table(TABLE_BOM)
    contract_table = qualified_table(TABLE_SUPPLIER_CONTRACT)

    existing_bom = existing_pairs(w, bom_table, ("fg_part_id", "component_part_id"))
    new_bom = [r for r in BOM_ROWS if (r[0], r[1]) not in existing_bom]
    print(f"dim_bom: {len(existing_bom)} existing, {len(new_bom)} new")
    if new_bom:
        values = ",\n  ".join(
            f"({sql_str(fg)}, {sql_str(comp)}, {qty}, current_timestamp())" for fg, comp, qty in new_bom
        )
        w.statement_execution.execute_statement(
            statement=f"INSERT INTO {bom_table} (fg_part_id, component_part_id, qty_per_unit, created_at) VALUES\n  {values}",
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
        )
        print(f"  inserted {len(new_bom)} row(s)")

    existing_contracts = existing_pairs(w, contract_table, ("part_id", "supplier_id"))
    new_contracts = [r for r in CONTRACT_ROWS if (r[0], r[1]) not in existing_contracts]
    print(f"dim_supplier_contract: {len(existing_contracts)} existing, {len(new_contracts)} new")
    if new_contracts:
        values = ",\n  ".join(
            f"({sql_str(part)}, {sql_str(supplier)}, {lead}, {moq}, {pack}, {cost}, {str(pref).upper()}, current_timestamp())"
            for part, supplier, lead, moq, pack, cost, pref in new_contracts
        )
        w.statement_execution.execute_statement(
            statement=(
                f"INSERT INTO {contract_table} "
                "(part_id, supplier_id, lead_time_days, moq, pack_size, unit_cost, is_preferred, created_at) VALUES\n  "
                f"{values}"
            ),
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
        )
        print(f"  inserted {len(new_contracts)} row(s)")

    delivery_table = qualified_fact_table("fact_supplier_delivery")
    existing_delivery_ids = {
        r[0] for r in run_query(w, f"SELECT DELIVERY_ID FROM {delivery_table} WHERE DELIVERY_ID LIKE 'DEMO-LEADTIME-%'")
    }
    candidate_deliveries = list(SUPPLIER_DELIVERY_ROWS) + _generate_supplier_delivery_rows(w)
    new_deliveries = [r for r in candidate_deliveries if r[0] not in existing_delivery_ids]
    print(f"fact_supplier_delivery (DEMO_SEED rows): {len(existing_delivery_ids)} existing, {len(new_deliveries)} new")
    if new_deliveries:
        supplier_ids = {r[1] for r in new_deliveries}
        supplier_keys = {
            row[0]: row[1]
            for row in run_query(
                w,
                f"SELECT SUPPLIER_ID, SUPPLIER_KEY FROM {qualified_dim_table('dim_supplier')} "
                f"WHERE IS_CURRENT = true AND SUPPLIER_ID IN ({', '.join(sql_str(s) for s in supplier_ids)})",
            )
        }
        max_key = int(run_query(w, f"SELECT COALESCE(MAX(SUPPLIER_DELIVERY_KEY), 0) FROM {delivery_table}")[0][0])
        rows_sql = []
        for i, (delivery_id, supplier_id, planned_date_key, delivery_date_key, qty) in enumerate(new_deliveries, start=1):
            delay_days = _date_key_diff(planned_date_key, delivery_date_key)
            supplier_key = supplier_keys[supplier_id]
            freight_cost = qty * 50
            # Derived, not hardcoded: a seeded set that mixes on-time and late
            # deliveries is what makes otd_rate a real number rather than 0,
            # and reliability_score is 70% otd_rate (see signal_board.py).
            on_time = delay_days <= 0
            otd_flag = "true" if on_time else "false"
            status = "ON_TIME" if on_time else "LATE"
            rows_sql.append(
                f"({max_key + i}, {sql_str(delivery_id)}, {delivery_date_key}, {planned_date_key}, "
                f"{supplier_key}, {WAREHOUSE_KEY_FOR_DEMO_DELIVERIES}, {qty}, {delay_days}, 0, 0, "
                f"{freight_cost}.00, {otd_flag}, {sql_str(status)}, current_timestamp(), 'DEMO_SEED')"
            )
        values = ",\n  ".join(rows_sql)
        w.statement_execution.execute_statement(
            statement=(
                f"INSERT INTO {delivery_table} "
                "(SUPPLIER_DELIVERY_KEY, DELIVERY_ID, DELIVERY_DATE_KEY, PLANNED_DATE_KEY, SUPPLIER_KEY, "
                "WAREHOUSE_KEY, QUANTITY, DELAY_DAYS, DAMAGED_QTY, SHORT_QTY, FREIGHT_COST, OTD_FLAG, "
                "DELIVERY_STATUS, DW_LOADED_AT, DW_SOURCE) VALUES\n  "
                f"{values}"
            ),
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
        )
        print(f"  inserted {len(new_deliveries)} row(s)")

    print("Done.")


def _part_index(part_id: str) -> int:
    return int(re.sub(r"\D", "", part_id))


def _generate_background_snapshot_rows(w: WorkspaceClient, used_part_ids: set[str]) -> list[tuple]:
    """One healthy row per part not already named above -- board breadth
    without inventing a signal. Deterministic (part-index-derived), not
    random, so a re-run of --rebuild-facts is reproducible."""
    parts = run_query(w, f"SELECT PART_ID, PART_TYPE, UNIT_COST FROM {qualified_dim_table('dim_part')} WHERE IS_CURRENT = true")
    warehouse_ids = sorted(r[0] for r in run_query(w, f"SELECT WAREHOUSE_ID FROM {qualified_dim_table('dim_warehouse')}"))

    base_by_type = {"ASSEMBLY": 700, "SUB-ASSEMBLY": 500, "COMPONENT": 350}
    rows = []
    for part_id, part_type, unit_cost in parts:
        if part_id in used_part_ids:
            continue
        warehouse_id = warehouse_ids[(_part_index(part_id) * 11) % len(warehouse_ids)]
        base = base_by_type.get(part_type, 400)
        max_stock = base
        safety_stock = int(base * 0.20)
        on_hand = int(base * 0.65)
        avg_daily = round(max_stock / 40.0, 2)
        rows.append((part_id, warehouse_id, on_hand, safety_stock, max_stock, avg_daily, float(unit_cost)))
    return rows


def rebuild_facts(w: WorkspaceClient) -> None:
    """DESTRUCTIVE: clear and rewrite the fact tables generate_sim_data.py
    used to own. See the module docstring for why and what's preserved."""
    snapshot_table = qualified_fact_table("fact_inventory_snapshot")
    transaction_table = qualified_fact_table("fact_inventory_transaction")
    procurement_table = qualified_fact_table("fact_procurement")
    plant_capacity_table = qualified_table("fact_plant_capacity")
    sim_events_table = qualified_table("sim_events")
    sim_pair_scenarios_table = qualified_table("sim_pair_scenarios")

    background_rows = _generate_background_snapshot_rows(w, {r[0] for r in _NAMED_SNAPSHOT_ROWS})
    all_snapshot_rows = [(p, wh, oh, sf, mx, burn, cost) for p, wh, oh, sf, mx, burn, cost, *_ in STORY_SNAPSHOT_ROWS] \
        + list(TRANSFER_SNAPSHOT_ROWS) + list(DEMAND_SHIFT_SNAPSHOT_ROWS) + background_rows

    # Hand-authored series first, then generated coverage for every other pair
    # with a real burn rate (see _generate_consumption_history for why).
    transaction_rows = list(DEMAND_SHIFT_TRANSACTION_ROWS) + _generate_consumption_history(
        all_snapshot_rows, {(r[0], r[1]) for r in DEMAND_SHIFT_TRANSACTION_ROWS}
    )

    part_ids = {r[0] for r in all_snapshot_rows} | {r[0] for r in transaction_rows} | {r[0] for r in PROCUREMENT_ROWS}
    warehouse_ids = {r[1] for r in all_snapshot_rows} | {r[1] for r in transaction_rows}
    part_keys = {
        row[0]: (row[1], float(row[2]))
        for row in run_query(
            w,
            f"SELECT PART_ID, PART_KEY, UNIT_COST FROM {qualified_dim_table('dim_part')} "
            f"WHERE IS_CURRENT = true AND PART_ID IN ({', '.join(sql_str(p) for p in part_ids)})",
        )
    }
    warehouse_keys = {
        row[0]: row[1]
        for row in run_query(
            w,
            f"SELECT WAREHOUSE_ID, WAREHOUSE_KEY FROM {qualified_dim_table('dim_warehouse')} "
            f"WHERE WAREHOUSE_ID IN ({', '.join(sql_str(wh) for wh in warehouse_ids)})",
        )
    }
    supplier_keys = {
        row[0]: row[1]
        for row in run_query(
            w,
            f"SELECT SUPPLIER_ID, SUPPLIER_KEY FROM {qualified_dim_table('dim_supplier')} "
            f"WHERE IS_CURRENT = true AND SUPPLIER_ID IN ({', '.join(sql_str(r[1]) for r in PROCUREMENT_ROWS)})",
        )
    }

    today_key = 20260902

    print("Clearing 6 fact table(s)...")
    for table in (snapshot_table, transaction_table, procurement_table, plant_capacity_table, sim_events_table, sim_pair_scenarios_table):
        w.statement_execution.execute_statement(statement=f"DELETE FROM {table}", warehouse_id=WAREHOUSE_ID, wait_timeout="30s")
    print("  cleared.")

    snapshot_values = []
    for i, (part_id, warehouse_id, on_hand, safety_stock, max_stock, avg_daily, unit_cost) in enumerate(all_snapshot_rows, start=1):
        part_key, _ = part_keys[part_id]
        warehouse_key = warehouse_keys[warehouse_id]
        days_of_supply = round(on_hand / avg_daily, 1) if avg_daily else 0.0
        stock_valuation = on_hand * unit_cost
        stockout_risk = "HIGH" if on_hand < safety_stock else "LOW"
        snapshot_values.append(
            f"({i}, {today_key}, {part_key}, {warehouse_key}, {on_hand}, {safety_stock}, {max_stock}, "
            f"0, {on_hand}, 0, 0, {days_of_supply}, {avg_daily}, NULL, {stock_valuation}, "
            f"{sql_str(stockout_risk)}, current_timestamp(), 'DEMO_SEED', NULL)"
        )
    w.statement_execution.execute_statement(
        statement=(
            f"INSERT INTO {snapshot_table} (INVENTORY_SNAPSHOT_KEY, SNAPSHOT_DATE_KEY, PART_KEY, WAREHOUSE_KEY, "
            "QUANTITY_ON_HAND, SAFETY_STOCK_QTY, MAX_STOCK_LEVEL, ALLOCATED_QTY, AVAILABLE_QTY, IN_TRANSIT_QTY, "
            "BLOCKED_QTY, DAYS_OF_SUPPLY, AVG_DAILY_CONSUMPTION, INVENTORY_TURNOVER_RATIO, STOCK_VALUATION, "
            "STOCKOUT_RISK, DW_LOADED_AT, DW_SOURCE, STOCK_ID) VALUES\n  " + ",\n  ".join(snapshot_values)
        ),
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="50s",
    )
    print(f"fact_inventory_snapshot: inserted {len(snapshot_values)} row(s)")

    txn_values = []
    for i, (part_id, warehouse_id, txn_date_key, qty) in enumerate(transaction_rows, start=1):
        part_key, unit_cost = part_keys[part_id]
        warehouse_key = warehouse_keys[warehouse_id]
        txn_values.append(
            f"({i}, {sql_str(f'DEMO-TXN-{i:04d}')}, {txn_date_key}, {part_key}, {warehouse_key}, "
            f"NULL, NULL, NULL, 'ISSUE', {qty}, {unit_cost}, {qty * unit_cost}, NULL, "
            "current_timestamp(), 'DEMO_SEED')"
        )
    w.statement_execution.execute_statement(
        statement=(
            f"INSERT INTO {transaction_table} (INVENTORY_TXN_KEY, TRANSACTION_ID, TRANSACTION_DATE_KEY, PART_KEY, "
            "WAREHOUSE_KEY, PRODUCTION_ORDER_ID, LINE_KEY, OPERATOR_EMPLOYEE_KEY, TRANSACTION_TYPE, QUANTITY, "
            "UNIT_COST, TRANSACTION_VALUE, BALANCE_AFTER_TXN, DW_LOADED_AT, DW_SOURCE) VALUES\n  "
            + ",\n  ".join(txn_values)
        ),
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="30s",
    )
    print(f"fact_inventory_transaction: inserted {len(txn_values)} row(s)")

    proc_values = []
    for i, (part_id, supplier_id, order_date_key, expected_date_key, order_qty, unit_rate, status) in enumerate(PROCUREMENT_ROWS, start=1):
        part_key, _ = part_keys[part_id]
        supplier_key = supplier_keys[supplier_id]
        total_amount = order_qty * unit_rate
        tax_amount = round(total_amount * 0.18, 2)
        proc_values.append(
            f"({i}, {sql_str(f'DEMO-PO-{i:03d}')}, {order_date_key}, {expected_date_key}, {part_key}, "
            f"{supplier_key}, {PLANT_KEY_FOR_DEMO_PROCUREMENT}, NULL, 'SCHEDULED', {sql_str(status)}, "
            f"{order_qty}, {unit_rate}, {total_amount}, 0, {order_qty}, {tax_amount}, "
            "current_timestamp(), 'DEMO_SEED')"
        )
    if proc_values:
        w.statement_execution.execute_statement(
            statement=(
                f"INSERT INTO {procurement_table} (PROCUREMENT_KEY, PURCHASE_ORDER_ID, ORDER_DATE_KEY, "
                "EXPECTED_DATE_KEY, PART_KEY, SUPPLIER_KEY, PLANT_KEY, BUYER_EMPLOYEE_KEY, PO_TYPE, STATUS, "
                "ORDER_QTY, UNIT_RATE, TOTAL_AMOUNT, RECEIVED_QTY, PENDING_QTY, TAX_AMOUNT, DW_LOADED_AT, "
                "DW_SOURCE) VALUES\n  " + ",\n  ".join(proc_values)
            ),
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
        )
    print(f"fact_procurement: inserted {len(proc_values)} row(s)")

    print("fact_plant_capacity / sim_events / sim_pair_scenarios: cleared, left empty (see module docstring)")
    print("Done. Run `databricks bundle run deploy_uc_functions -t dev` next to refresh the board.")


def report(w: WorkspaceClient) -> None:
    """Print a one-screen readiness checklist: does each intelligence layer
    have at least one live candidate right now? Run this before a demo
    instead of re-deriving these queries by hand."""
    func_prefix = qualified_table("").rstrip(".")

    print("Layer readiness (live gold_dev.supply_chain_analytics data):\n")
    for label, call in REPORT_SCAN_FUNCTIONS:
        rows = run_query(w, f"SELECT COUNT(*) FROM {func_prefix}.{call}")
        count = int(rows[0][0]) if rows else 0
        flag = "OK" if count >= 3 else ("THIN" if count > 0 else "EMPTY")
        print(f"  [{flag:5}] {label}: {count} row(s)")

    print()
    diverse_rows = run_query(
        w, f"SELECT signal_type, part_id, warehouse_id, decision_value "
           f"FROM {func_prefix}.rank_priority_actions_diverse() ORDER BY decision_value DESC"
    )
    covered = {r[0] for r in diverse_rows}
    print(f"  rank_priority_actions_diverse: {len(diverse_rows)} signal type(s) with a live top candidate")
    for signal_type, part_id, warehouse_id, decision_value in diverse_rows:
        print(f"    {signal_type:20} {part_id} @ {warehouse_id}  decision_value={decision_value}")
    missing = [s for s in ALL_SIGNAL_TYPES if s not in covered]
    if missing:
        print(f"  MISSING signal type(s) today: {', '.join(missing)} -- a demo run right now would surface "
              f"only {len(covered)} line(s) instead of up to {len(ALL_SIGNAL_TYPES)}.")
    else:
        print("  All known signal types have a live top candidate -- a demo run today surfaces the full set.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--report", action="store_true", help="Print layer readiness instead of seeding")
    parser.add_argument(
        "--prune-quotes",
        action="store_true",
        help="DESTRUCTIVE: drop every quote except PRESERVED_QUOTE_IDS and the most recent "
             "line-bearing one (see --keep-recent). The demo reset for the approval queue.",
    )
    parser.add_argument(
        "--keep-recent",
        type=int,
        default=1,
        help="With --prune-quotes, how many recent line-bearing quotes to keep (default 1)",
    )
    parser.add_argument(
        "--rebuild-facts",
        action="store_true",
        help="DESTRUCTIVE: clear and rewrite fact_inventory_snapshot/transaction/procurement, "
        "fact_plant_capacity, sim_events, sim_pair_scenarios. See the module docstring.",
    )
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)

    if args.prune_quotes:
        prune_quotes(w, keep_recent=args.keep_recent)
    elif args.rebuild_facts:
        rebuild_facts(w)
    elif args.report:
        report(w)
    else:
        seed(w)


if __name__ == "__main__":
    main()
