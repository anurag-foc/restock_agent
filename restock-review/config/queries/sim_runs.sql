-- Simulation results, newest batch first.
--
-- Every figure here is SIMULATED, not measured savings: the pipeline scored against a
-- generated world whose demand we wrote (docs/simulation_feature_design.md §2.2). The page
-- must label it as such, and must lead on the difference between engines rather than on the
-- absolute number -- the absolute is dominated by the output budget and the scenario, the
-- difference is the part that says something about the estimator.
--
-- BATCH_ID groups the engines that were compared against one world. Comparing rows across
-- batches is only meaningful when SETTINGS_JSON matches, since a net value computed under a
-- 14% holding rate is a different number from one computed under 20%.
SELECT
  RUN_ID,
  BATCH_ID,
  RUN_TS,
  LABEL,
  CONSUMPTION_MODEL,
  BUDGET,
  SETTINGS_JSON,
  PAIRS_TOTAL,
  PAIRS_AT_RISK,
  CAUGHT,
  MISSED,
  FALSE_ALARMS,
  CORRECTLY_QUIET,
  DETECTOR_CAUGHT,
  DETECTOR_FALSE_ALARMS,
  FINDINGS_DETECTED,
  FINDINGS_SELECTED,
  VALUE_DELIVERED,
  VALUE_MISSED,
  VALUE_WASTED,
  NET_VALUE,
  DETECTOR_NET_VALUE,
  BUDGET_COST,
  RECALL,
  PRECISION,
  DETECTOR_RECALL,
  DETECTOR_PRECISION,
  UNSCORED_FINDINGS
FROM gold_dev_analytics.supply_chain_analytics.sim_run
ORDER BY RUN_TS DESC, CONSUMPTION_MODEL
LIMIT 60
