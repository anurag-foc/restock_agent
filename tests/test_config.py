from agentic_restock import config


def test_qualified_table_uses_catalog_and_schema():
    assert config.qualified_table("inventory_stock_level") == (
        f"{config.CATALOG}.{config.SCHEMA}.inventory_stock_level"
    )


def test_table_name_constants_are_unique():
    names = [
        config.TABLE_INVENTORY_STOCK_LEVEL,
        config.TABLE_THRESHOLD_CONFIG,
        config.TABLE_CONSUMPTION_HISTORY,
        config.TABLE_OPEN_REQUEST,
        config.TABLE_RESTOCK_REQUESTS,
    ]
    assert len(names) == len(set(names))
