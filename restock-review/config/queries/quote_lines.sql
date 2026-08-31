-- @param quoteId STRING = QT-20260828-C8C9
SELECT
  frr.RESTOCK_REQUEST_KEY,
  frr.QUOTE_ID,
  frr.RESTOCK_REQUEST_ID,
  dp.PART_ID,
  dp.PART_NAME,
  dw.WAREHOUSE_ID,
  drs.REQUEST_STATUS,
  drs.URGENCY_LEVEL,
  drs.DECISION,
  frr.CURRENT_STOCK_QTY,
  frr.REORDER_POINT_QTY,
  frr.REQUESTED_QTY,
  frr.CONFIRMED_QTY,
  frr.VARIANCE_QTY,
  frr.REQUESTED_DATE_KEY,
  frr.DECISION_DATE_KEY,
  frr.FULFILLED_DATE_KEY
FROM gold_dev.supply_chain_analytics.fact_restock_request frr
JOIN gold_dev.dim.dim_part dp ON frr.PART_KEY = dp.PART_KEY AND dp.IS_CURRENT = true
JOIN gold_dev.dim.dim_warehouse dw ON frr.WAREHOUSE_KEY = dw.WAREHOUSE_KEY
JOIN gold_dev.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
WHERE frr.QUOTE_ID = :quoteId
ORDER BY frr.RESTOCK_REQUEST_KEY
