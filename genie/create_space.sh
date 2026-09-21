#!/usr/bin/env bash
# Recreate the "Insurance Claims and Payments" Genie space from genie_agent.json.
#
# Prereqs: dim_claim + fact_payments tables exist (pipeline/notebooks/05_structured_facts.py),
# a Pro/Serverless SQL warehouse, and Databricks Assistant enabled in the workspace.
#
# Usage:
#   PROFILE=<cli-profile> WAREHOUSE_ID=<sql-warehouse-id> ./create_space.sh
# Optional overrides (defaults are the reference workspace's catalog/schema):
#   CATALOG=my_catalog SCHEMA=my_schema TITLE="..." PARENT_PATH=/Workspace/Users/you@co.com/genie_spaces
set -euo pipefail

PROFILE="${PROFILE:?set PROFILE to your Databricks CLI profile}"
WAREHOUSE_ID="${WAREHOUSE_ID:?set WAREHOUSE_ID to a Pro/Serverless SQL warehouse id}"
CATALOG="${CATALOG:-fins_genai}"
SCHEMA="${SCHEMA:-claims_multimodal_kb}"
TITLE="${TITLE:-Insurance Claims and Payments}"

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(databricks current-user me --profile "$PROFILE" -o json | jq -r '.userName')"
PARENT_PATH="${PARENT_PATH:-/Workspace/Users/${USER_NAME}/genie_spaces}"

# Remap the reference catalog.schema to the target (no-op if unchanged).
TMP="$(mktemp)"
python3 - "$DIR/genie_agent.json" "$TMP" "${CATALOG}.${SCHEMA}" <<'PY'
import sys
src, out, target = sys.argv[1], sys.argv[2], sys.argv[3]
open(out, "w").write(open(src).read().replace("fins_genai.claims_multimodal_kb", target))
PY

databricks workspace mkdirs "$PARENT_PATH" --profile "$PROFILE"

SS="$(jq -c '.' "$TMP" | jq -Rs '.')"
databricks genie create-space --profile "$PROFILE" --json "$(jq -n \
  --arg wh "$WAREHOUSE_ID" --arg title "$TITLE" --arg pp "$PARENT_PATH" --argjson ss "$SS" \
  '{warehouse_id:$wh, title:$title, parent_path:$pp, serialized_space:$ss}')"

rm -f "$TMP"
echo
echo "Created. Copy the returned space_id into:"
echo "  - adjuster-console/databricks.yml   (resources → genie_space → space_id)"
echo "  - adjuster-console/agent_server/agent.py   (GENIE_SPACE_ID)"
