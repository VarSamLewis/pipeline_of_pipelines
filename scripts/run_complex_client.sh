#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
CLIENT_CODE="${CLIENT_CODE:-complex}"
EXAMPLES_DIR="${EXAMPLES_DIR:-examples/complex_client}"

echo "==> Ensuring client '$CLIENT_CODE' exists"
CLIENT_RESPONSE=$(curl -s -m 5 "$BASE_URL/clients/$CLIENT_CODE" -w "\n%{http_code}")
CLIENT_HTTP=$(echo "$CLIENT_RESPONSE" | tail -1)
if [ "$CLIENT_HTTP" = "200" ]; then
  echo "Client '$CLIENT_CODE' already exists"
  echo "$CLIENT_RESPONSE" | sed '$d' | jq .
else
  curl -s -X POST "$BASE_URL/clients" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"Complex Client\", \"code\": \"$CLIENT_CODE\"}" | jq .
fi

echo "==> Uploading target schema"
curl -s -X POST "$BASE_URL/clients/$CLIENT_CODE/target-schema" \
  -F "schema_file=@$EXAMPLES_DIR/target_schema.json" | jq .

echo "==> Ingesting folder '$EXAMPLES_DIR'"
INGEST_RESPONSE=$(
  curl -s -X POST "$BASE_URL/clients/$CLIENT_CODE/ingest-folder" \
    -F "folder_path=$EXAMPLES_DIR" \
    -F "label=complex_fixture"
)
echo "$INGEST_RESPONSE" | jq .

RAW_FILE_IDS=$(echo "$INGEST_RESPONSE" | jq -r '.raw_file_ids | @json')

echo "==> Creating mapping spec"
TARGET_SCHEMA=$(cat "$EXAMPLES_DIR/target_schema.json")
SPEC_RESPONSE=$(
  curl -s -X POST "$BASE_URL/clients/$CLIENT_CODE/mapping-specs" \
    -H "Content-Type: application/json" \
    -d "{\"source_raw_file_ids\": $RAW_FILE_IDS, \"target_schema\": $TARGET_SCHEMA, \"description\": \"complex client mapping\"}"
)
echo "$SPEC_RESPONSE" | jq .

SPEC_ID=$(echo "$SPEC_RESPONSE" | jq -r '.id')

echo "==> Proposing mappings for spec $SPEC_ID"
curl -s -X POST "$BASE_URL/mapping-specs/$SPEC_ID/propose" | jq .

echo "==> Approving spec $SPEC_ID"
curl -s -X POST "$BASE_URL/mapping-specs/$SPEC_ID/approve" \
  -H "Content-Type: application/json" \
  -d '{"reviewer": "data-guardian"}' | jq .

echo "==> Generating output folder"
curl -s -X POST "$BASE_URL/mapping-specs/$SPEC_ID/output-folder" | jq .

echo ""
echo "==> Done. Artifacts available at:"
echo "  $BASE_URL/output-folders/$SPEC_ID/pipeline.py"
echo "  $BASE_URL/output-folders/$SPEC_ID/mapping.json"
echo "  $BASE_URL/output-folders/$SPEC_ID/results.csv"
