from agentic_restock import config
from agentic_restock.jobs.lakeflow_trigger import build_coarse_check_query


def test_query_uses_default_catalog_and_schema():
    query = build_coarse_check_query()
    assert config.qualified_table(config.TABLE_INVENTORY_STOCK_LEVEL) in query
    assert config.qualified_table(config.TABLE_THRESHOLD_CONFIG) in query


def test_query_respects_explicit_catalog_and_schema_override():
    query = build_coarse_check_query(catalog="my_cat", schema="my_schema")
    assert "my_cat.my_schema.inventory_stock_level" in query
    assert "my_cat.my_schema.threshold_config_table" in query
    assert config.CATALOG not in query
    assert config.SCHEMA not in query


def test_query_filters_active_thresholds_at_or_below_reorder_point():
    query = build_coarse_check_query()
    assert "tct.is_active = true" in query
    assert "isl.current_stock_qty <= tct.reorder_point_qty" in query


def test_query_joins_on_item_and_warehouse():
    query = build_coarse_check_query()
    assert "isl.item_id = tct.item_id" in query
    assert "isl.warehouse_id = tct.warehouse_id" in query
