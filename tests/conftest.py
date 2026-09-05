"""Shared fixtures for the dataset generator tests.

`simulate_all()` runs the reorder loop over ~278 pairs x 1100 days and takes ~10s. It is
deterministic and read-only, so it is built once per session rather than once per test module —
three module-scoped copies had pushed the suite past 100s.
"""

import pytest


@pytest.fixture(scope="session")
def histories():
    """Every pair's simulated history. Deterministic; treat as read-only."""
    from agentic_restock.generation import replenishment

    return replenishment.simulate_all()


@pytest.fixture(scope="session")
def delivery_records(histories):
    from agentic_restock.generation import supplier_facts

    return supplier_facts.delivery_records(histories)


@pytest.fixture(scope="session")
def lead_summary(delivery_records):
    from agentic_restock.generation import supplier_facts

    return supplier_facts.observed_lead_summary(delivery_records)
