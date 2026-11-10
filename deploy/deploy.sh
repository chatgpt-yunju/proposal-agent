#!/usr/bin/env bash
# 写方案智能体 · 云主机部署脚本（Linux）
#
#   GH_TOKEN=ghp_xxx ./deploy/deploy.sh install     # 首次部署（拉代码 + 建环境 + 装依赖 + 起服务）
#   ./deploy/deploy.sh update                       # 只更新代码并重启
#   ./deploy/deploy.sh start | stop | restart | status | logs
#
# 约定：代码目录 $APP_DIR（默认 ~/proposal-agent），
#       data/（SQLite + 向量库 + 导出产物）和 .env 永不覆盖。
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/proposal-agent}"
CODE_DIR="${CODE_DIR:-$HOME/proposal-agent-src}"
REPO_URL="${REPO_URL:-https://github.com/chatgpt-yunju/proposal-agent.git}"
PORT="${PORT:-8000}"
PID_FILE="$APP_DIR/data/uvicorn.pid"
LOG_FILE="$APP_DIR/data/server.log"
BRANCH="${BRANCH:-main}"

say() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }

fetch_code() {
  [ -n "${GH_TOKEN:-}" ] || die "缺少 GH_TOKEN（GitHub personal access token），私有仓库必须带上"
  local url="${REPO_URL/https:\/\//https://$GH_TOKEN@}"
  if [ -d "$CODE_DIR/.git" ]; then
    say "更新已有源码副本 $CODE_DIR"
    git -C "$CODE_DIR" fetch -q origin "$BRANCH"
    git -C "$CODE_DIR" reset -q --hard "origin/$BRANCH"
  else
    say "克隆代码到 $CODE_DIR"
    rm -rf "$CODE_DIR"
    git clone -q --depth 1 -b "$BRANCH" "$url" "$CODE_DIR"
  fi
  say "源码版本：$(git -C "$CODE_DIR" log -1 --pretty='%h %s')"
}

sync_code() {
  mkdir -p "$APP_DIR/data"
  say "同步 app/ 与依赖清单（保留 data/ 和 .env）"
  rsync -a --delete --exclude '__pycache__' "$CODE_DIR/app/" "$APP_DIR/app/"
  for f in requirements.txt Dockerfile docker-compose.yml .env.example README.md; do
    [ -f "$CODE_DIR/$f" ] && cp "$CODE_DIR/$f" "$APP_DIR/$f"
  done
  if [ ! -f "$APP_DIR/.env" ]; then
    cp "$CODE_DIR/.env.example" "$APP_DIR/.env"
    say "已生成 $APP_DIR/.env，请填入 LLM_API_KEY / EMBEDDING_API_KEY 后再启动"
  fi
}

install_deps() {
  cd "$APP_DIR"
  if [ ! -x .venv/bin/python ]; then
    say "创建虚拟环境 .venv"
    python3 -m venv .venv
  fi
  say "安装 Python 依赖（首次约 2~5 分钟）"
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt ${PIP_INDEX:+--index-url "$PIP_INDEX"}
  ./.venv/bin/python - <<'PY'
import fastapi, uvicorn, qdrant_client, docx, pymupdf, jieba, langgraph, numpy
print("依赖自检通过：fastapi", fastapi.__version__)
PY
}

check_fonts() {
  # PDF 导出依赖中文字体，缺字会渲染成空白/方框
  if ! fc-list :lang=zh 2>/dev/null | head -1 | grep -q .; then
    say "警告：系统没有中文字体，导出的 PDF 中文会丢失。请安装："
    say "  sudo apt-get install -y fonts-noto-cjk   （Debian/Ubuntu）"
  else
    say "中文字体：$(fc-list :lang=zh | wc -l) 个可用"
  fi
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

wait_health() {
  local i
  for i in $(seq 1 90); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1; then
      say "服务已就绪：$(curl -s -m 3 "http://127.0.0.1:$PORT/api/version")"
      return 0
    fi
    is_running || { say "进程已退出，日志末尾："; tail -40 "$LOG_FILE" 2>/dev/null || true; return 1; }
    sleep 2
  done
  say "180 秒内未就绪，日志末尾："; tail -40 "$LOG_FILE" 2>/dev/null || true; return 1
}

start() {
  cd "$APP_DIR"
  if is_running; then say "已在运行（pid $(cat "$PID_FILE")），跳过启动"; return 0; fi
  [ -f .env ] || die "缺少 $APP_DIR/.env"
  say "启动 uvicorn（host 0.0.0.0:$PORT）"
  setsid nohup ./.venv/bin/python -u -m uvicorn app.main:app \
      --host 0.0.0.0 --port "$PORT" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  wait_health
}

stop() {
  if is_running; then
    say "停止 pid $(cat "$PID_FILE")"
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 3
    kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
  else
    say "未在运行"
  fi
  rm -f "$PID_FILE"
}

status() {
  if is_running; then
    say "运行中 pid $(cat "$PID_FILE")"
    curl -s -m 5 "http://127.0.0.1:$PORT/api/version" || true; echo
    curl -s -m 5 "http://127.0.0.1:$PORT/api/projects" | head -c 200 || true; echo
    ps -o pid,rss,etime,cmd -p "$(cat "$PID_FILE")" 2>/dev/null || true
  else
    say "未运行"; tail -20 "$LOG_FILE" 2>/dev/null || true
  fi
}

case "${1:-install}" in
  install)  fetch_code; sync_code; install_deps; check_fonts; start ;;
  update)   fetch_code; sync_code; stop; start ;;
  start)    start ;;
  stop)     stop ;;
  restart)  stop; start ;;
  status)   status ;;
  logs)     tail -n "${2:-60}" "$LOG_FILE" ;;
  *)        die "未知命令：$1（可用：install / update / start / stop / restart / status / logs）" ;;
esac
