-- @param quoteId STRING = QT-20260828-C8C9
SELECT
  frr.RESTOCK_REQUEST_KEY,
  frr.QUOTE_ID,
  frr.RESTOCK_REQUEST_ID,
  dp.PART_ID,
  dp.PART_NAME,
  dw.WAREHOUSE_ID,
  frr.ACTION_TYPE,
  frr.FINDING_TYPE,
  frr.SUBJECT_KEY,
  donor.WAREHOUSE_ID AS SOURCE_WAREHOUSE_ID,
  sup.SUPPLIER_ID AS RECOMMENDED_SUPPLIER_ID,
  frr.EXPOSURE_AT_DECISION,
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
  frr.FULFILLED_DATE_KEY,
  frr.NOTE,
  frr.DECISION_REASON
FROM gold_dev_analytics.supply_chain_analytics.fact_restock_request frr
-- LEFT, not INNER. Three of the eight finding types are about a supplier rather than a part,
-- and LEADTIME_SIGNAL carries neither a PART_KEY nor a WAREHOUSE_KEY (nullable since
-- docs/schema_changes_gold_dev_analytics.md 1.5). An inner join drops those lines silently --
-- the quote header would say 4 lines and this screen would render 3, with nothing to show one
-- was missing.
LEFT JOIN gold_dev_analytics.dim.dim_part dp ON frr.PART_KEY = dp.PART_KEY AND dp.IS_CURRENT = true
LEFT JOIN gold_dev_analytics.dim.dim_warehouse dw ON frr.WAREHOUSE_KEY = dw.WAREHOUSE_KEY
LEFT JOIN gold_dev_analytics.dim.dim_warehouse donor ON frr.SOURCE_WAREHOUSE_KEY = donor.WAREHOUSE_KEY
LEFT JOIN gold_dev_analytics.dim.dim_supplier sup
  ON frr.RECOMMENDED_SUPPLIER_KEY = sup.SUPPLIER_KEY AND sup.IS_CURRENT = true
JOIN gold_dev_analytics.dim.dim_request_status drs ON frr.REQUEST_STATUS_KEY = drs.REQUEST_STATUS_KEY
WHERE frr.QUOTE_ID = :quoteId
ORDER BY frr.RESTOCK_REQUEST_KEY
