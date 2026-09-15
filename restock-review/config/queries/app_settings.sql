-- Settings in force right now: the newest row per key.
--
-- `app_settings` is append-only (see src/agentic_restock/settings.py), so change history
-- is the table itself and this view is just the top of each key's stack. A key that has
-- never been written is absent rather than null -- the client fills it from the same
-- defaults the pipeline uses, so an untouched table and a table of explicit defaults
-- behave identically.
SELECT
  setting_key,
  setting_value,
  updated_at,
  updated_by
FROM (
  SELECT
    setting_key,
    setting_value,
    updated_at,
    updated_by,
    ROW_NUMBER() OVER (
      PARTITION BY setting_key, scope_type, COALESCE(scope_id, '')
      ORDER BY updated_at DESC
    ) AS rn
  FROM gold_dev_analytics.supply_chain_analytics.app_settings
  WHERE scope_type = 'GLOBAL'
)
WHERE rn = 1
