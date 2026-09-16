"""The inbound-quantity bug that was hiding how well the pipeline does.

`simulation/frames.py` claims in its own docstring to mirror `jobs/positions.py`'s queries. Its
in-transit adapter did not. It summed `PENDING_QTY` by `PART_KEY` alone and applied no status
filter, where the query groups by `(PART_KEY, WAREHOUSE_KEY)` and filters
`STATUS IN ('ISSUED','PARTIAL')`. Every warehouse holding a part was therefore credited with the
whole network's open orders -- available came out at roughly four times on-hand. Days of cover
followed, and S1, whose entire test is whether cover outlasts the lead time, went quiet on pairs
that were genuinely short.

Nothing failed. The simulation just reported a worse pipeline than the one that exists, and a
truth rule computed off the same inflated availability agreed with it -- plausible output, no
error, which is the shape of every bug in this repo's history worth recording.

Underneath it sat a second one in the data: `fact_procurement.WAREHOUSE_KEY` was populated by a
one-off backfill through `PLANT_KEY`, and `generation/procurement.py` assigned the default plant
to any warehouse with no plant link -- which is every regional DC. 528 DC-bound orders were
recorded against WH001. The generator now emits the destination it always knew, which also means
the column survives a regenerate instead of reverting to NULL.
"""

from __future__ import annotations

import pytest

from agentic_restock.generation import dataset
from agentic_restock.simulation import frames as sim_frames
from agentic_restock.simulation import run


@pytest.fixture(scope="module")
def world():
    return run.world()


def test_open_orders_land_in_one_warehouse_each(world):
    """The thing the old code got wrong, stated as a sum.

    Inbound per part across all warehouses must equal the pending quantity on that part's open
    POs -- not that figure multiplied by however many warehouses happen to stock it.
    """
    data = dataset.build()
    expected: dict[int, int] = {}
    for row in data["fact_procurement"]:
        if row["STATUS"] not in sim_frames.OPEN_PO_STATUSES or row["PENDING_QTY"] <= 0:
            continue
        expected[row["PART_KEY"]] = expected.get(row["PART_KEY"], 0) + row["PENDING_QTY"]

    actual: dict[int, int] = {}
    for row in world["position"].itertuples(index=False):
        if row.IN_TRANSIT_QTY:
            actual[row.PART_KEY] = actual.get(row.PART_KEY, 0) + int(row.IN_TRANSIT_QTY)

    assert actual == expected


def test_every_warehouse_has_an_inbound_pipeline(world):
    """Regional DCs had none at all: they have no linked plant, so their orders fell through to
    the plant-link branch and were delivered to the default plant's store instead. A warehouse
    that can never have stock on its way is not a warehouse any detector can reason about -- it
    reads as permanently, structurally short."""
    inbound = world["position"].groupby("WAREHOUSE_ID")["IN_TRANSIT_QTY"].sum()
    silent = sorted(inbound[inbound == 0].index)
    assert not silent, f"no open orders are destined for {silent}"


def test_in_transit_is_not_larger_than_on_hand_across_the_network(world):
    """A blunt guard on the failure mode rather than on its cause. The old sum produced a
    network holding ~2.8x more stock in transit than on the ground, which no manufacturer runs
    and which silently suppressed every cover-based test."""
    position = world["position"]
    assert position["IN_TRANSIT_QTY"].sum() < position["QUANTITY_ON_HAND"].sum()


def test_every_generated_order_carries_its_destination():
    """`PLANT_KEY` cannot express one: a regional DC has no plant. Without this column the
    backfill resolves every DC order onto the default plant's store, and 'all rows resolved'
    reads as 'all rows resolved correctly' when it is not the same claim."""
    for row in dataset.build()["fact_procurement"]:
        assert row.get("WAREHOUSE_KEY")
