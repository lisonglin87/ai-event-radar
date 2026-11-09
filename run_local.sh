#!/usr/bin/env bash
# 本地运行：自动加载 .env 后调用 src.main
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

exec python -m src.main "$@"
