"""Deterministic estimators (E1 burn, E2 lead time).

Pure functions over arrays -- no Spark, no Databricks -- so the Phase 2 gate runs locally
against the same series the generator produces. See docs/intelligence_layer_design.md 2.
"""
