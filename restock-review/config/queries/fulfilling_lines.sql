SELECT
  frr.RESTOCK_REQUEST_KEY,
  frr.QUOTE_ID,
  dp.PART_ID,
  dp.PART_NAME,
  dw.WAREHOUSE_ID,
  drs.URGENCY_LEVEL,
  frr.REQUESTED_QTY,
  frr.CONFIRMED_QTY,
  frr.VARIANCE_QTY,
  frr.NOTE,
  frr.DECISION_DATE_KEY
FROM gold_dev.supply_chain_analytics.fact_restock_request frr
JOIN gold_dev.dim.dim_part dp ON frr.PART_KEY = dp.PART_KEY AND dp.IS_CURRENT = true
JOIN gold_dev.dim.dim_warehouse dw ON frr.WAREHOUSE_KEY = dw.WAREHOUSE_KEY
JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
WHERE drs.REQUEST_STATUS = 'FULFILLING'
ORDER BY
  CASE drs.URGENCY_LEVEL WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
  frr.DECISION_DATE_KEY DESC
LIMIT 100
