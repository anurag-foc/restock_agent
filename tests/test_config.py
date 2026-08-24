from agentic_restock import config


def test_qualified_table_uses_own_catalog_and_schema():
    assert config.qualified_table("quote_metadata") == (f"{config.CATALOG}.{config.SCHEMA}.quote_metadata")


def test_qualified_dim_table_uses_gold_catalog_and_dim_schema():
    assert config.qualified_dim_table("dim_part") == (f"{config.GOLD_CATALOG}.{config.DIM_SCHEMA}.dim_part")


def test_qualified_fact_table_uses_gold_catalog_and_facts_schema():
    assert config.qualified_fact_table("fact_inventory_snapshot") == (
        f"{config.GOLD_CATALOG}.{config.FACTS_SCHEMA}.fact_inventory_snapshot"
    )


def test_dim_table_name_constants_are_unique():
    names = [
        config.TABLE_DIM_PART,
        config.TABLE_DIM_WAREHOUSE,
        config.TABLE_DIM_SUPPLIER,
        config.TABLE_DIM_PLANT,
        config.TABLE_DIM_REQUEST_STATUS,
    ]
    assert len(names) == len(set(names))


def test_fact_table_name_constants_are_unique():
    names = [
        config.TABLE_FACT_INVENTORY_SNAPSHOT,
        config.TABLE_FACT_INVENTORY_TRANSACTION,
        config.TABLE_FACT_PROCUREMENT,
        config.TABLE_FACT_RESTOCK_REQUEST,
        config.TABLE_METRICS_INVENTORY_SNAPSHOT,
        config.TABLE_METRICS_INVENTORY_TRANSACTION,
        config.TABLE_METRICS_PROCUREMENT,
        config.TABLE_METRICS_RESTOCK_REQUEST,
    ]
    assert len(names) == len(set(names))
