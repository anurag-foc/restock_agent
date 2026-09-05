"""Decision history — `fact_restock_request`.

Without this table nuance-8 suppression is dead code and `STALLED_COMMITMENT` can never fire,
leaving only two live signal types — the exact collapse this redesign exists to fix
(docs/dataset_generator_spec.md §7.1).

Two constraints pull against each other, and getting them the wrong way round breaks the whole
dataset:

- **Suppression needs open commitments.** Every branch of the freshness rule has to be
  represented: rejected, pending-and-fresh, pending-and-stale, approved-and-fresh,
  approved-and-stale, completed.
- **Open commitments suppress findings.** A commitment placed on one of the F1-F11 pairs would
  suppress the finding it was planted to demonstrate. So the history goes on *quiet* pairs,
  except for the four stale commitments, which are placed on genuinely short pairs precisely so
  `STALLED_COMMITMENT` has something to re-surface — a stale request against a healthy part
  re-surfaces nothing, and would test the rule against a row the ranking already dropped.

Status keys are not resolved here. `dim_request_status` is a legitimate 17-row dimension keyed by
(status, urgency) with hash surrogate keys, and it is not being regenerated — so rows carry
`_request_status` / `_urgency_level` for the loader to join on, the same way suppliers carry
`_archetype`.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_restock.generation import contracts, dates, demand, entities
from agentic_restock.generation.replenishment import PairHistory
from agentic_restock.generation.scenarios import expect_finding

# Freshness thresholds the suppression rule uses. A pending request re-surfaces after a
# defensible PM turnaround; an approved one after its own lead time plus a grace buffer. Kept
# here so `validate_plan()` can check the planted ages actually straddle them -- a "stale" spec
# aged 1 day would silently test the fresh branch instead.
PENDING_STALE_DAYS = 2
APPROVED_GRACE_DAYS = 3

REVIEWER_EMPLOYEE_KEY = -1

# How many planted commitments sit on genuinely short pairs so STALLED_COMMITMENT can re-surface
# them. Must cover every spec whose `demonstrates` says "re-surfaces".
STALE_COMMITMENT_COUNT = 4


@dataclass(frozen=True)
class DecisionSpec:
    """One planted decision: which status, how old, and what it should demonstrate."""

    status: str
    urgency: str
    requested_days_ago: int
    decided_days_ago: int | None
    fulfilled_days_ago: int | None
    demonstrates: str


def decision_plan() -> list[DecisionSpec]:
    """The status matrix, sized so every suppression branch has at least one example."""
    return [
        # --- live queue: what the review app shows a PM right now ------------
        DecisionSpec("PENDING_APPROVAL", "CRITICAL", 1, None, None, "fresh: suppressed"),
        DecisionSpec("PENDING_APPROVAL", "HIGH", 1, None, None, "fresh: suppressed"),
        DecisionSpec("PENDING_APPROVAL", "MEDIUM", 0, None, None, "fresh: suppressed"),
        DecisionSpec("NEEDS_REVIEW", "HIGH", 1, None, None, "fresh: suppressed"),
        # --- gone stale: must re-surface as STALLED_COMMITMENT ---------------
        DecisionSpec("PENDING_APPROVAL", "CRITICAL", 9, None, None, "stale >2d: re-surfaces"),
        DecisionSpec("NEEDS_REVIEW", "MEDIUM", 14, None, None, "stale >2d: re-surfaces"),
        # --- decided and in execution ----------------------------------------
        DecisionSpec("APPROVED", "HIGH", 6, 4, None, "fresh approval: suppressed"),
        DecisionSpec("FULFILLING", "CRITICAL", 12, 10, None, "in execution: suppressed"),
        DecisionSpec("FULFILLING", "HIGH", 95, 90, None, "stale execution: re-surfaces"),
        DecisionSpec("APPROVED", "MEDIUM", 120, 115, None, "stale approval: re-surfaces"),
        # --- closed ----------------------------------------------------------
        DecisionSpec("REJECTED", "LOW", 40, 39, None, "permanently suppressed"),
        DecisionSpec("REJECTED", "LOW", 150, 148, None, "permanently suppressed"),
        DecisionSpec("COMPLETED", "HIGH", 60, 58, 30, "ledger: realised outcome"),
        DecisionSpec("COMPLETED", "MEDIUM", 100, 97, 70, "ledger: realised outcome"),
        DecisionSpec("COMPLETED", "LOW", 140, 137, 110, "ledger: realised outcome"),
        DecisionSpec("COMPLETED", "CRITICAL", 175, 173, 150, "ledger: realised outcome"),
    ]


def validate_plan() -> list[str]:
    """Check the planted ages actually straddle the freshness thresholds.

    A spec labelled "re-surfaces" whose age sits inside the fresh window would exercise the
    opposite branch, and the label would be the only evidence anything was wrong.
    """
    problems: list[str] = []
    for spec in decision_plan():
        should_resurface = "re-surfaces" in spec.demonstrates
        if spec.status in ("PENDING_APPROVAL", "NEEDS_REVIEW"):
            is_stale = spec.requested_days_ago > PENDING_STALE_DAYS
        elif spec.status in ("APPROVED", "FULFILLING"):
            # Compared against a generous lead time; the exact figure is per-part at detection.
            is_stale = (spec.decided_days_ago or 0) > 60 + APPROVED_GRACE_DAYS
        else:
            is_stale = False
        if should_resurface != is_stale:
            problems.append(
                f"{spec.status} aged {spec.requested_days_ago}d is labelled "
                f"'{spec.demonstrates}' but computes stale={is_stale}"
            )
    return problems


def _eligible_pairs(histories: dict[tuple[str, str], PairHistory]) -> list[tuple[str, str]]:
    """Quiet pairs that can carry a decision without suppressing a planted finding."""
    return sorted(
        key
        for key, history in histories.items()
        if not expect_finding(*key)
        and not history.params.finding_ids
        and history.params.history_cohort == "full"
        and contracts.preferred_supplier(key[0]) is not None
    )


def _stall_pairs(histories: dict[tuple[str, str], PairHistory]) -> list[tuple[str, str]]:
    """Short pairs deliberately given a stale commitment.

    `STALLED_COMMITMENT` can only fire where a commitment sits on a row that *would* otherwise
    be raised — a stale request against a healthy part re-surfaces nothing. These are chosen
    from short pairs that carry no planted finding, so nothing else is disturbed.
    """
    candidates = sorted(
        key
        for key, history in histories.items()
        if not expect_finding(*key)
        and not history.params.finding_ids
        and history.closing_qty < history.params.safety_stock
        and contracts.preferred_supplier(key[0]) is not None
    )
    return _spread(candidates, STALE_COMMITMENT_COUNT)


def _spread(candidates: list[tuple[str, str]], count: int) -> list[tuple[str, str]]:
    """Take `count` entries spaced across the list rather than the first `count`.

    Pairs are sorted by (part, warehouse), so consecutive entries are usually the *same part* at
    different warehouses — taking a prefix put the whole decision history onto four parts.
    """
    if not candidates:
        return []
    stride = max(1, len(candidates) // count)
    picked = candidates[::stride][:count]
    return picked or candidates[:count]


def restock_request_rows(histories: dict[tuple[str, str], PairHistory]) -> list[dict]:
    """`fact_restock_request` rows, one per part-line, grouped into quotes."""
    part_keys = {p["PART_ID"]: p["PART_KEY"] for p in entities.parts()}
    warehouse_keys = {w["WAREHOUSE_ID"]: w["WAREHOUSE_KEY"] for w in entities.warehouses()}
    supplier_keys = {s["SUPPLIER_ID"]: s["SUPPLIER_KEY"] for s in entities.suppliers()}

    plan = decision_plan()
    eligible = _eligible_pairs(histories)
    stall_pairs = _stall_pairs(histories)

    # EVERY spec that should re-surface goes onto a genuinely short pair -- a stale request
    # against a healthy part re-surfaces nothing, so it would test the suppression rule against
    # a row the ranking would have dropped anyway. The rest land on quiet pairs, spread across
    # parts so no single part carries the whole history.
    stale_indices = [i for i, spec in enumerate(plan) if "re-surfaces" in spec.demonstrates]
    quiet_needed = len(plan) - len(stale_indices)
    quiet_pairs = _spread(eligible, quiet_needed) or eligible

    assignments: list[tuple[DecisionSpec, tuple[str, str]]] = []
    cursor = 0
    for index, spec in enumerate(plan):
        if index in stale_indices and stall_pairs:
            assignments.append((spec, stall_pairs[stale_indices.index(index) % len(stall_pairs)]))
            continue
        assignments.append((spec, quiet_pairs[cursor % len(quiet_pairs)]))
        cursor += 1

    rows: list[dict] = []
    key = 1
    for index, (spec, (part_id, warehouse_id)) in enumerate(assignments):
        history = histories[(part_id, warehouse_id)]
        params = history.params
        n = len(history.issues)
        supplier_id = contracts.preferred_supplier(part_id)

        requested_day = n - 1 - spec.requested_days_ago
        quote_date = dates.date_for(requested_day, n)
        quote_id = f"QT-{dates.date_key(quote_date)}-{index // 2:02d}"

        requested_qty = max(1, params.max_stock - history.closing_qty)
        confirmed = (
            requested_qty
            if spec.status in ("APPROVED", "FULFILLING", "COMPLETED")
            else 0
        )
        lead_days = demand._lead_days_for(part_id)

        rows.append(
            {
                "RESTOCK_REQUEST_KEY": key,
                "QUOTE_ID": quote_id,
                "RESTOCK_REQUEST_ID": f"RR-{key:07d}",
                "REQUESTED_DATE_KEY": dates.day_index_key(requested_day, n),
                "DECISION_DATE_KEY": (
                    dates.day_index_key(n - 1 - spec.decided_days_ago, n)
                    if spec.decided_days_ago is not None
                    else None
                ),
                "FULFILLED_DATE_KEY": (
                    dates.day_index_key(n - 1 - spec.fulfilled_days_ago, n)
                    if spec.fulfilled_days_ago is not None
                    else None
                ),
                "PART_KEY": part_keys[part_id],
                "WAREHOUSE_KEY": warehouse_keys[warehouse_id],
                "SUPPLIER_KEY": supplier_keys[supplier_id],
                "REVIEWER_EMPLOYEE_KEY": REVIEWER_EMPLOYEE_KEY,
                "CURRENT_STOCK_QTY": history.closing_qty,
                "REORDER_POINT_QTY": params.safety_stock,
                "REQUESTED_QTY": requested_qty,
                "CONFIRMED_QTY": confirmed,
                "VARIANCE_QTY": confirmed - requested_qty if confirmed else 0,
                "APPROVAL_LAG_HRS": (
                    round((spec.requested_days_ago - spec.decided_days_ago) * 24, 2)
                    if spec.decided_days_ago is not None
                    else None
                ),
                "FULFILLMENT_LAG_HRS": (
                    round((spec.decided_days_ago - spec.fulfilled_days_ago) * 24, 2)
                    if spec.decided_days_ago is not None and spec.fulfilled_days_ago is not None
                    else None
                ),
                "NOTE": _note_for(spec, lead_days),
                "DW_SOURCE": entities.DW_SOURCE,
                # Resolved by the loader against dim_request_status.
                "_request_status": spec.status,
                "_urgency_level": spec.urgency,
                "_demonstrates": spec.demonstrates,
            }
        )
        key += 1

    return rows


def _note_for(spec: DecisionSpec, lead_days: int) -> str | None:
    """The PM's free-text reasoning. Only decided lines have one."""
    if spec.status == "REJECTED":
        return "Rejected: covered by an existing order, no further spend approved."
    if spec.status == "COMPLETED":
        return f"Received and put away. Lead time ran close to the contracted {lead_days} days."
    if spec.status in ("APPROVED", "FULFILLING"):
        return "Approved: exposure justified the spend."
    return None


def quote_ids(rows: list[dict]) -> list[str]:
    return sorted({row["QUOTE_ID"] for row in rows})
