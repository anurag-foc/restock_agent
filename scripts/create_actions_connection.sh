#!/usr/bin/env bash
# Create (or recreate) the UC HTTP Connection that exposes the restock-review
# app's MCP server to the Supervisor Agent as the `restockify_actions` tool.
#
# Why this exists: the Supervisor's action tools (persist_quote,
# send_human_review, fulfill_restock_request) cannot be UC functions --
# a UC SQL function body rejects DML outright, and a UC Python UDF has no
# network egress (both verified). Databricks' supported path for agent action
# tools is an MCP server behind a UC HTTP Connection, which is what this
# creates. The server itself lives in restock-review/server/mcp.ts.
#
# Usage:
#   ./scripts/create_actions_connection.sh [dev|prod]
#
# Env vars:
#   DATABRICKS_PROFILE   ~/.databrickscfg profile (default: anurag-r)
#   SP_CLIENT_ID         Service principal application id used to call the app
#   SP_SECRET_SCOPE      Databricks secret scope holding the SP secret
#   SP_SECRET_KEY        Key within that scope (default: restockify-sp-secret)
#   CONNECTION_NAME      UC connection name (default: restockify_actions_mcp)
#
# Prerequisites:
#   1. `databricks bundle deploy` has run, so the app exists and has a URL.
#   2. A service principal exists, has CAN_USE on the app, and its OAuth
#      secret is stored in a Databricks secret scope:
#        databricks secrets create-scope <scope>
#        databricks secrets put-secret <scope> <key> --string-value <secret>
#      The secret is referenced via secret(), never inlined -- an inline
#      literal would land in the statement text, query history, and
#      SHOW CREATE CONNECTION output.

set -euo pipefail

TARGET="${1:-dev}"
PROFILE="${DATABRICKS_PROFILE:-anurag-r}"
CONNECTION_NAME="${CONNECTION_NAME:-restockify_actions_mcp}"
SP_SECRET_KEY="${SP_SECRET_KEY:-restockify-sp-secret}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${SP_CLIENT_ID:?Set SP_CLIENT_ID to the service principal application id}"
: "${SP_SECRET_SCOPE:?Set SP_SECRET_SCOPE to the secret scope holding the SP secret}"

echo "==> Resolving deployed app URL (target: $TARGET)"
APP_URL="$(databricks apps get restock-review --profile "$PROFILE" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
if [ -z "$APP_URL" ]; then
  echo "Could not resolve the restock-review app URL -- has the bundle been deployed?" >&2
  exit 1
fi
APP_HOST="${APP_URL#https://}"
APP_HOST="${APP_HOST%%/*}"
echo "    app host: $APP_HOST"

WORKSPACE_HOST="$(databricks auth env --profile "$PROFILE" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["env"]["DATABRICKS_HOST"])' || true)"
WORKSPACE_HOST="${WORKSPACE_HOST:-https://dbc-371e9acf-b9ec.cloud.databricks.com}"
TOKEN_ENDPOINT="${WORKSPACE_HOST%/}/oidc/v1/token"
echo "    token endpoint: $TOKEN_ENDPOINT"

echo "==> Creating UC connection '$CONNECTION_NAME'"
databricks api post /api/2.0/sql/statements --profile "$PROFILE" --json "$(cat <<JSON
{
  "warehouse_id": "d2533a75c1bd9265",
  "wait_timeout": "50s",
  "statement": "CREATE OR REPLACE CONNECTION \`${CONNECTION_NAME}\` TYPE HTTP OPTIONS (host '${APP_URL%/}', port '443', base_path '/api/mcp', client_id '${SP_CLIENT_ID}', client_secret secret('${SP_SECRET_SCOPE}','${SP_SECRET_KEY}'), oauth_scope 'all-apis', token_endpoint '${TOKEN_ENDPOINT}', is_mcp_connection 'true')"
}
JSON
)" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("status",{}); print("   ", s.get("state")); e=s.get("error"); print("    ERROR:", e.get("message")) if e else None'

echo
echo "==> Verifying the connection can reach the MCP server (tools/list)"
databricks api post /api/2.0/sql/statements --profile "$PROFILE" --json "$(cat <<JSON
{
  "warehouse_id": "d2533a75c1bd9265",
  "wait_timeout": "50s",
  "statement": "SELECT http_request(conn => '${CONNECTION_NAME}', method => 'POST', path => '', json => '{\"jsonrpc\":\"2.0\",\"method\":\"tools/list\",\"id\":1}')"
}
JSON
)" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("status",{}); print("   ", s.get("state")); r=d.get("result",{}).get("data_array"); print("   ", str(r)[:400]) if r else None; e=s.get("error"); print("    ERROR:", e.get("message")) if e else None'

echo
echo "==> Granting the Supervisor Agent's principal access"
echo "    Run this once, substituting the agent's service principal:"
echo "      GRANT USE CONNECTION ON CONNECTION \`${CONNECTION_NAME}\` TO \`<agent_sp>\`;"
echo
echo "Next: python3 scripts/ensure_supervisor_agent.py --profile $PROFILE --target $TARGET"
