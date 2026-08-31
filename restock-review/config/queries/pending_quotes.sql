SELECT
  qm.quote_id,
  qm.created_at,
  qm.teams_sent_at,
  COUNT(*) AS total_lines,
  SUM(CASE WHEN drs.REQUEST_STATUS = 'PENDING_APPROVAL' THEN 1 ELSE 0 END) AS pending_lines,
  SUM(CASE WHEN drs.REQUEST_STATUS = 'APPROVED' THEN 1 ELSE 0 END) AS approved_lines,
  SUM(CASE WHEN drs.REQUEST_STATUS = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_lines,
  MIN(CASE drs.URGENCY_LEVEL WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END) AS top_urgency_rank
FROM gold_dev.supply_chain_analytics.quote_metadata qm
JOIN gold_dev.supply_chain_analytics.fact_restock_request frr ON frr.QUOTE_ID = qm.quote_id
JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
GROUP BY qm.quote_id, qm.created_at, qm.teams_sent_at
HAVING SUM(CASE WHEN drs.REQUEST_STATUS = 'PENDING_APPROVAL' THEN 1 ELSE 0 END) > 0
ORDER BY top_urgency_rank ASC, qm.created_at DESC
LIMIT 50
