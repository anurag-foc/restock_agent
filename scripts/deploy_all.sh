#!/usr/bin/env bash
# Deploy everything for the Inventory Intelligence bundle in one shot.
#
# Usage:
#   ./scripts/deploy_all.sh [dev|prod]
#
# Env vars:
#   DATABRICKS_PROFILE  ~/.databrickscfg profile to use (default: anurag-r)
#   SEED=true           also (re)run schema_bootstrap -- DESTRUCTIVE, wipes
#                        and reseeds quote_metadata (the Teams/Review-App
#                        companion table; real inventory/procurement/restock
#                        data lives in Data Engineering's gold_dev and is
#                        never touched by this repo). Off by default so this
#                        script is safe to re-run against a target with real
#                        in-flight data (quote_metadata rows, etc.).
#
# What it does, in order:
#   1. databricks bundle validate
#   2. databricks bundle deploy               (Jobs, Genie Space)
#   3. databricks bundle run schema_bootstrap  (only if SEED=true)
#   4. databricks bundle run deploy_uc_functions (idempotent CREATE OR REPLACE)
#   5. scripts/ensure_supervisor_agent.py      (idempotent create-or-reuse +
#                                                syncs the endpoint name into
#                                                resources/jobs/lakeflow_trigger_job.yml)
#   6. databricks bundle deploy again          (only matters if step 5 changed
#                                                the job yaml; harmless no-op
#                                                otherwise)

set -euo pipefail

TARGET="${1:-dev}"
PROFILE="${DATABRICKS_PROFILE:-anurag-r}"
SEED="${SEED:-false}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

profile_args=(--profile "$PROFILE")

echo "==> [1/6] Validating bundle (target: $TARGET, profile: $PROFILE)"
databricks bundle validate -t "$TARGET" "${profile_args[@]}"

echo "==> [2/6] Deploying bundle resources"
databricks bundle deploy -t "$TARGET" "${profile_args[@]}"

if [ "$SEED" = "true" ]; then
  echo "==> [3/6] Seeding quote_metadata (schema_bootstrap) -- this wipes existing rows"
  databricks bundle run schema_bootstrap -t "$TARGET" "${profile_args[@]}"
else
  echo "==> [3/6] Skipping schema_bootstrap (set SEED=true to reseed quote_metadata)"
fi

echo "==> [4/6] Deploying Unity Catalog functions"
databricks bundle run deploy_uc_functions -t "$TARGET" "${profile_args[@]}"

echo "==> [5/6] Ensuring Supervisor Agent + tools exist"
# The Supervisor's `inventory_intelligence_actions` tool attaches the mcp-inventory-actions
# app directly (app authorization, tool_type="app") -- no UC Connection or
# service principal/secret-scope setup needed. Step 2 already deployed the
# app; this just wires it into the Supervisor's tool set.
#
# One-time manual step (not automated here): grant the app's own auto-
# provisioned service principal Unity Catalog access -- see
# docs/agent_bricks_mapping.md. Without it, persist_quote/send_human_review/
# fulfill_restock_request will fail with a UC permission error, not an auth
# error, the first time the Supervisor calls them.
APP_SP="$(databricks apps get "${ACTIONS_APP_NAME:-mcp-inventory-actions}" --profile "$PROFILE" -o json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("service_principal_client_id") or d.get("service_principal_name") or "")' 2>/dev/null || true)"
if [ -n "$APP_SP" ]; then
  echo "    mcp-inventory-actions service principal: $APP_SP"
  echo "    Ensure it has UC grants on gold_dev.supply_chain_analytics (fact_restock_request,"
  echo "    quote_metadata, fact_inventory_snapshot) and gold_dev.dim -- see docs/agent_bricks_mapping.md."
else
  echo "    WARNING: could not resolve the mcp-inventory-actions app/service principal."
  echo "    It must have Unity Catalog grants before the action tools will work."
fi
python3 scripts/ensure_supervisor_agent.py --profile "$PROFILE" --target "$TARGET"

echo "==> [6/6] Re-deploying (picks up any Supervisor Agent endpoint change)"
databricks bundle deploy -t "$TARGET" "${profile_args[@]}"

echo
echo "==> Deployment summary"
databricks bundle summary -t "$TARGET" "${profile_args[@]}"
