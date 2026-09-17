-- What each approach actually put in front of a person, on the latest run.
--
-- The counts in sim_runs say how many were right. This says what they *were*, and the shape is
-- the argument the page is making: four items spanning four different kinds of problem, only one
-- of which is a shortage. A reorder-point rule's twenty rows are twenty of the same sentence,
-- and the two side by side say more than any ratio does.
--
-- EXPOSURE is money at risk on a warehouse we generated. It is SIMULATED and is never measured
-- savings (docs/simulation_feature_design.md §2.2) -- the page must label it so. EXPOSURE_BASIS
-- carries the derivation in words, because there is no single formula across the eight finding
-- types and the figure cannot be re-derived at display time.
SELECT
  RUN_ID,
  BATCH_ID,
  CONSUMPTION_MODEL,
  RANK,
  FINDING_TYPE,
  SUBJECT_ID,
  ACTION_TYPE,
  ACTION_DETAIL,
  EXPOSURE,
  ACTION_COST,
  DECISION_VALUE,
  EXPOSURE_BASIS,
  CONFIDENCE
FROM gold_dev_analytics.supply_chain_analytics.sim_selection
ORDER BY BATCH_ID DESC, CONSUMPTION_MODEL, RANK
LIMIT 400
