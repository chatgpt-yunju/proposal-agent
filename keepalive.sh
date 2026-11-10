#!/usr/bin/env bash
# 30s 一次健康检查，掉线自动拉起（沙箱内没有 systemd，用它兜底）
cd "$(dirname "$0")"
while true; do
  if ! curl -sf -m 5 http://127.0.0.1:8000/api/version >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] health check failed -> restart" >> data/keepalive.log
    bash start.sh >> data/keepalive.log 2>&1
  fi
  sleep 30
done
