-- Counts and one worked example per finding type, per arm, on the latest run. This is the
-- benchmark chart's data.
--
-- Only types an arm actually produced get a row -- a reorder-point rule that only ever raises
-- STOCKOUT_RISK has one row here, not eight with seven zeros. That is the honest way to show a
-- tool has no concept of a kind of problem at all, rather than a count of zero for it.
--
-- EXAMPLE_REASONING is plain English, generated once in simulation/reasoning.py and stored
-- rather than recomputed on display -- the reasoning is the claim, and a claim that regenerates
-- itself on every page load is not the same claim twice. EXAMPLE_EXPOSURE is money at risk on a
-- warehouse we generated: SIMULATED, never measured savings.
--
-- ACCURACY_NOTE is only populated for the three shortage-addressing types (Running out,
-- Assembly line blocked, Move stock instead of buying) -- the only three with a ground truth to
-- check against. It is empty for the other five, deliberately: there is no true answer yet for
-- dead capital, supplier drift, demand shift, supplier economics or MOQ, and printing a number
-- there would be inventing one. Also populated for the incumbent ERP arms' own STOCKOUT_RISK
-- rows, so their hit rate is shown on the same footing as ours.
SELECT
  RUN_ID,
  BATCH_ID,
  RUN_TS,
  LABEL,
  CONSUMPTION_MODEL,
  FINDING_TYPE,
  COUNT,
  EXAMPLE_SUBJECT,
  EXAMPLE_REASONING,
  EXAMPLE_EXPOSURE,
  ACCURACY_NOTE
FROM gold_dev_analytics.supply_chain_analytics.sim_type_summary
ORDER BY BATCH_ID DESC, CONSUMPTION_MODEL, COUNT DESC
LIMIT 200
