-- @param quoteId STRING = QT-20260828-C8C9
SELECT
  quote_id,
  summary_report,
  teams_message_id,
  teams_sent_at,
  databricks_preview_url,
  decision_comments,
  created_by,
  created_at,
  updated_at
FROM gold_dev.supply_chain_analytics.quote_metadata
WHERE quote_id = :quoteId
