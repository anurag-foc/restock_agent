-- The eleven planted problems and how the latest run was graded against each.
--
-- This is the trust surface, and it is a different question from sim_runs. `sim_run` scores
-- "will these pairs run short", which covers three of the eight scanners; this table asks, per
-- planted fault, whether the system found it -- and it is the only place the other five say
-- anything at all.
--
-- The answer sheet was fixed in generation/scenarios.py before the detectors existed, and every
-- row names the real part, warehouse or supplier it was checked on, so a viewer can open any
-- line and check it by hand. That auditability is the whole point: the score is not the
-- evidence, the named subject is.
--
-- IS_NEGATIVE rows are the ones that matter most. A benchmark of positives only scores highest
-- for a system that flags everything, which is the alert fatigue this product exists to remove.
SELECT
  BATCH_ID,
  RUN_TS,
  LABEL,
  FINDING_ID,
  NAME,
  PROVES,
  FINDING_TYPE,
  IS_NEGATIVE,
  RESULT,
  SUBJECT,
  DETAIL,
  SHOWN_TO_PM
FROM gold_dev_analytics.supply_chain_analytics.sim_benchmark
ORDER BY RUN_TS DESC, FINDING_ID
LIMIT 220
