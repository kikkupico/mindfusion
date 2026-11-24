#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

PORT=8765
HOST="0.0.0.0"
OPEN_BROWSER=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --no-browser)
      OPEN_BROWSER=0
      shift
      ;;
    --open-browser)
      OPEN_BROWSER=1
      shift
      ;;
    *)
      shift
      ;;
  esac
done

URL="http://${HOST}:${PORT}"

# Check if already running
if curl -sS "${URL}/api/state" >/dev/null 2>&1; then
  echo "[MindFusion] Server is already running at ${URL}"
  curl -sS -X POST "${URL}/api/agent/heartbeat" \
    -H "Content-Type: application/json" \
    -d "{\"agent_name\": \"${MINDFUSION_AGENT_NAME:-Agent}\", \"mode\": \"agent_bridge\"}" >/dev/null 2>&1 || true
  if [[ "${OPEN_BROWSER}" -eq 1 ]]; then
    open "${URL}" >/dev/null 2>&1 || python3 -m webbrowser "${URL}" >/dev/null 2>&1 || true
  fi
  exit 0
fi

cd "${PROJECT_ROOT}"
mkdir -p "${PROJECT_ROOT}/.mindfusion"

EXTRA_ARGS=("--host" "${HOST}" "--port" "${PORT}" "--agent-connected")
if [[ "${OPEN_BROWSER}" -eq 1 ]]; then
  EXTRA_ARGS+=("--open-browser")
fi

nohup python3 -m mindfusion.server "${EXTRA_ARGS[@]}" > "${PROJECT_ROOT}/.mindfusion/server.log" 2>&1 &
SERVER_PID=$!
echo "${SERVER_PID}" > "${PROJECT_ROOT}/.mindfusion/server.pid"

# Wait for server readiness
for _ in {1..25}; do
  if curl -sS "${URL}/api/state" >/dev/null 2>&1; then
    echo "[MindFusion] Started server (PID ${SERVER_PID}) at ${URL}"
    exit 0
  fi
  sleep 0.2
done

echo "[MindFusion] Server launched (PID ${SERVER_PID}); check ${PROJECT_ROOT}/.mindfusion/server.log if needed."
