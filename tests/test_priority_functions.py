from agentic_restock.jobs.priority_functions import (
    FUNCTION_NAMES,
    build_function_statements,
)


def test_rank_priority_actions_diverse_is_registered():
    assert "rank_priority_actions_diverse" in FUNCTION_NAMES


def test_function_names_and_statements_are_aligned():
    statements = build_function_statements()
    assert len(statements) == len(FUNCTION_NAMES)


def test_rank_priority_actions_diverse_partitions_by_signal_type():
    statements = build_function_statements()
    by_name = dict(zip(FUNCTION_NAMES, statements))
    diverse_sql = by_name["rank_priority_actions_diverse"]
    assert "PARTITION BY signal_type" in diverse_sql
    assert "rn_in_type = 1" in diverse_sql
