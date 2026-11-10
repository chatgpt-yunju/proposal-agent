#!/usr/bin/env bash
# 幂等启动：已在跑就直接返回
set -uo pipefail
cd "$(dirname "$0")"
if curl -sf -m 3 http://127.0.0.1:8000/api/version >/dev/null 2>&1; then
  echo "proposal-agent already running on :8000"; exit 0
fi
pkill -f "uvicorn app.main:app" 2>/dev/null
sleep 1
setsid nohup ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> data/server.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  if curl -sf -m 3 http://127.0.0.1:8000/api/version >/dev/null 2>&1; then
    echo "started, ready in ${i}s"; exit 0
  fi
  sleep 1
done
echo "FAILED to become ready in 60s, tail of log:"; tail -20 data/server.log; exit 1
