#!/usr/bin/env bash
# Deploy everything for the Restockify bundle in one shot.
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
# The Supervisor's `restockify_actions` tool points at a UC HTTP Connection
# wrapping the deployed app's MCP server. The connection has to exist first --
# it is a one-time setup step because it needs service principal OAuth
# credentials in a secret scope, so it is deliberately NOT re-run here:
#   SP_CLIENT_ID=... SP_SECRET_SCOPE=... ./scripts/create_actions_connection.sh $TARGET
if ! databricks connections get "${CONNECTION_NAME:-restockify_actions_mcp}" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "    WARNING: UC connection '${CONNECTION_NAME:-restockify_actions_mcp}' not found."
  echo "    The Supervisor's action tools (persist_quote / send_human_review /"
  echo "    fulfill_restock_request) will not work until it exists. Create it with:"
  echo "      SP_CLIENT_ID=... SP_SECRET_SCOPE=... ./scripts/create_actions_connection.sh $TARGET"
fi
python3 scripts/ensure_supervisor_agent.py --profile "$PROFILE" --target "$TARGET"

echo "==> [6/6] Re-deploying (picks up any Supervisor Agent endpoint change)"
databricks bundle deploy -t "$TARGET" "${profile_args[@]}"

echo
echo "==> Deployment summary"
databricks bundle summary -t "$TARGET" "${profile_args[@]}"
