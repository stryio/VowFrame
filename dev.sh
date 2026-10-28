#!/usr/bin/env bash
# Start backend (auto-reload) and Vite dev server together; Ctrl+C stops both.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$ROOT/backend/.venv/bin/uvicorn" ]; then
  echo "缺少后端虚拟环境：backend/.venv（见 backend/README.md）" >&2
  exit 1
fi
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "安装前端依赖…"
  (cd "$ROOT/frontend" && npm install)
fi

cleanup() {
  trap - INT TERM EXIT
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(cd "$ROOT/backend" && exec .venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --reload --reload-dir app) &
(cd "$ROOT/frontend" && exec npx vite) &

echo "前端：http://localhost:5173   后端：http://127.0.0.1:8000   按 Ctrl+C 全部停止"
# Exit as soon as either process dies, so a crashed backend is not hidden.
wait -n
