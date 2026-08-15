#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  smoke_api.sh --base-url URL --api-key KEY [--input-url URL] [--channel-id ID] [--exercise-transcode]

Examples:
  smoke_api.sh --base-url http://127.0.0.1:18096 --api-key secret
  smoke_api.sh --base-url http://192.168.1.100:18096 --api-key secret \
    --input-url http://192.168.1.1:7088/rtp/239.1.1.1:5002 --channel-id smoke-demo
  smoke_api.sh --base-url http://192.168.1.100:18096 --api-key secret \
    --input-url http://192.168.1.1:7088/rtp/239.1.1.1:5002 --channel-id smoke-demo \
    --exercise-transcode

Checks:
  1. GET / and GET /api/health/details
  2. GET /api/config and GET /api/status
  3. Optional POST /api/probe when --input-url is provided
  4. Optional POST /api/transcode/start -> heartbeat -> stop when --exercise-transcode is provided
EOF
}

require_python() {
  command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required" >&2
    exit 1
  }
}

HTTP_STATUS=""
HTTP_BODY=""
STARTED_CHANNEL_ID=""

json_field() {
  local field="$1"
  python3 -c '
import json, sys
field = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception as exc:
    raise SystemExit(f"response is not valid JSON: {exc}")
value = data
for part in field.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
print("" if value is None else value)
' "$field"
}

assert_json_true() {
  local field="$1"
  python3 -c '
import json, sys
field = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception as exc:
    raise SystemExit(f"response is not valid JSON: {exc}")
value = data
for part in field.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is not True:
    raise SystemExit(f"{field} is not true: {value!r}")
' "$field"
}

request() {
  local method="$1"
  local url="$2"
  local body="${3-}"
  local tmp
  local err
  tmp="$(mktemp /tmp/iptvtranscoder-smoke-body.XXXXXX)"
  err="$(mktemp /tmp/iptvtranscoder-smoke-err.XXXXXX)"
  local status
  if [[ -n "$body" ]]; then
    if ! status="$(curl -sS -o "$tmp" -w "%{http_code}" -X "$method" "$url" \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      --data "$body" 2>"$err")"; then
      echo "Transport error from $url" >&2
      cat "$err" >&2 || true
      rm -f "$tmp" "$err"
      return 1
    fi
  else
    if ! status="$(curl -sS -o "$tmp" -w "%{http_code}" -X "$method" "$url" \
      -H "X-API-Key: $API_KEY" 2>"$err")"; then
      echo "Transport error from $url" >&2
      cat "$err" >&2 || true
      rm -f "$tmp" "$err"
      return 1
    fi
  fi
  HTTP_STATUS="$status"
  HTTP_BODY="$(cat "$tmp")"
  rm -f "$tmp" "$err"
  if [[ "$HTTP_STATUS" -lt 200 || "$HTTP_STATUS" -ge 300 ]]; then
    echo "HTTP $HTTP_STATUS from $url" >&2
    [[ -n "$HTTP_BODY" ]] && printf '%s\n' "$HTTP_BODY" >&2
    return 1
  fi
  printf '%s' "$HTTP_BODY"
}

cleanup_started_channel() {
  [[ -n "$STARTED_CHANNEL_ID" ]] || return 0
  curl -sS -o /dev/null -X POST "$BASE_URL/api/transcode/$STARTED_CHANNEL_ID/stop" \
    -H "X-API-Key: $API_KEY" || true
}

BASE_URL=""
API_KEY=""
INPUT_URL=""
CHANNEL_ID="smoke-demo"
EXERCISE_TRANSCODE="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="${2-}"; shift 2 ;;
    --api-key) API_KEY="${2-}"; shift 2 ;;
    --input-url) INPUT_URL="${2-}"; shift 2 ;;
    --channel-id) CHANNEL_ID="${2-}"; shift 2 ;;
    --exercise-transcode) EXERCISE_TRANSCODE="1"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -n "$BASE_URL" && -n "$API_KEY" ]] || {
  usage
  exit 1
}

require_python
BASE_URL="${BASE_URL%/}"
trap cleanup_started_channel EXIT

echo "[1/5] GET /"
root_json="$(request GET "$BASE_URL/" "")"
printf '%s' "$root_json" | assert_json_true ok

echo "[2/5] GET /api/health/details"
health_json="$(request GET "$BASE_URL/api/health/details")"
printf '%s' "$health_json" | assert_json_true ok

echo "[3/5] GET /api/config"
config_json="$(request GET "$BASE_URL/api/config")"
printf '%s' "$config_json" | assert_json_true ok

echo "[4/5] GET /api/status"
status_json="$(request GET "$BASE_URL/api/status")"
printf '%s' "$status_json" | assert_json_true ok

if [[ -n "$INPUT_URL" ]]; then
  echo "[5/5] POST /api/probe"
  probe_body="$(python3 - "$CHANNEL_ID" "$INPUT_URL" <<'PY'
import json, sys
print(json.dumps({
    "channel_id": sys.argv[1],
    "input_url": sys.argv[2],
    "operation": "qsv_h264",
}))
PY
)"
  probe_json="$(request POST "$BASE_URL/api/probe" "$probe_body")"
  printf '%s' "$probe_json" | assert_json_true ok
  echo "Probe suggested operation: $(printf '%s' "$probe_json" | json_field suggested_operation)"
else
  echo "[5/5] Skipped /api/probe because --input-url was not provided"
fi

if [[ "$EXERCISE_TRANSCODE" == "1" ]]; then
  [[ -n "$INPUT_URL" ]] || {
    echo "--exercise-transcode requires --input-url" >&2
    exit 1
  }
  echo "[extra] POST /api/transcode/start"
  start_body="$(python3 - "$CHANNEL_ID" "$INPUT_URL" <<'PY'
import json, sys
print(json.dumps({
    "channel_id": sys.argv[1],
    "input_url": sys.argv[2],
    "operation": "qsv_h264",
}))
PY
)"
  start_json="$(request POST "$BASE_URL/api/transcode/start" "$start_body")"
  printf '%s' "$start_json" | assert_json_true ok
  STARTED_CHANNEL_ID="$CHANNEL_ID"
  echo "[extra] POST /api/transcode/$CHANNEL_ID/heartbeat"
  heartbeat_ok="0"
  hb_json=""
  for attempt in 1 2 3; do
    hb_json="$(request POST "$BASE_URL/api/transcode/$CHANNEL_ID/heartbeat" '{}')"
    hb_state="$(printf '%s' "$hb_json" | json_field state)"
    hb_reason="$(printf '%s' "$hb_json" | json_field reason)"
    if [[ "$hb_state" == "starting" || "$hb_reason" == "starting" || "$hb_reason" == "missing_playlist" || "$hb_reason" == "empty_playlist" || "$hb_reason" == "missing_segments" ]]; then
      echo "Heartbeat warmup state: state=${hb_state:-<none>} reason=${hb_reason:-<none>} (attempt $attempt/3)"
      sleep 1
      continue
    fi
    hb_ok="$(printf '%s' "$hb_json" | json_field ok)"
    if [[ "$hb_ok" == "True" ]]; then
      heartbeat_ok="1"
      break
    fi
    echo "Unexpected heartbeat response" >&2
    printf '%s\n' "$hb_json" >&2
    exit 1
  done
  if [[ "$heartbeat_ok" != "1" ]]; then
    echo "Heartbeat did not become healthy within retry window" >&2
    printf '%s\n' "$hb_json" >&2
    exit 1
  fi
  echo "[extra] POST /api/transcode/$CHANNEL_ID/stop"
  stop_json="$(request POST "$BASE_URL/api/transcode/$CHANNEL_ID/stop" '{}')"
  printf '%s' "$stop_json" | assert_json_true ok
  STARTED_CHANNEL_ID=""
fi

echo "Smoke OK"
