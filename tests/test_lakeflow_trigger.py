from agentic_restock import config
from agentic_restock.jobs.lakeflow_trigger import build_coarse_check_query


def test_query_uses_default_gold_catalog_and_schemas():
    query = build_coarse_check_query()
    assert config.qualified_fact_table(config.TABLE_FACT_INVENTORY_SNAPSHOT) in query
    assert config.qualified_dim_table(config.TABLE_DIM_PART) in query
    assert config.qualified_dim_table(config.TABLE_DIM_WAREHOUSE) in query


def test_query_respects_explicit_catalog_and_schema_overrides():
    query = build_coarse_check_query(
        gold_catalog="my_cat", dim_schema="my_dim_schema", facts_schema="my_facts_schema"
    )
    assert "my_cat.my_facts_schema.fact_inventory_snapshot" in query
    assert "my_cat.my_dim_schema.dim_part" in query
    assert "my_cat.my_dim_schema.dim_warehouse" in query
    assert config.GOLD_CATALOG not in query
    assert f"{config.GOLD_CATALOG}.{config.DIM_SCHEMA}" not in query
    assert f"{config.GOLD_CATALOG}.{config.FACTS_SCHEMA}" not in query


def test_query_takes_latest_snapshot_per_part_and_warehouse():
    query = build_coarse_check_query()
    assert "ROW_NUMBER() OVER (" in query
    assert "PARTITION BY PART_KEY, WAREHOUSE_KEY" in query
    assert "ls.rn = 1" in query


def test_query_filters_active_below_safety_stock():
    query = build_coarse_check_query()
    assert "dp.LIFECYCLE_STATUS = 'ACTIVE'" in query
    assert "dw.OPERATIONAL_STATUS = 'ACTIVE'" in query
    assert "ls.QUANTITY_ON_HAND <= ls.SAFETY_STOCK_QTY" in query


def test_query_joins_on_part_and_warehouse_surrogate_keys():
    query = build_coarse_check_query()
    assert "ls.PART_KEY = dp.PART_KEY" in query
    assert "ls.WAREHOUSE_KEY = dw.WAREHOUSE_KEY" in query
