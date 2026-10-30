#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
RUNTIME_DIR="${TAPAY_RUNTIME_DIR:-/home/jovyan/tapay-runtime}"
LLAMA_SERVER="${TAPAY_LLAMA_SERVER:-$RUNTIME_DIR/llama.cpp/build/bin/llama-server}"
MODEL_FILE="${TAPAY_MODEL_FILE:-$RUNTIME_DIR/models/Qwen3-8B-Q4_K_M.gguf}"
mkdir -p data logs
export TAPAY_MODEL_BASE_URL=http://127.0.0.1:8081/v1
export TAPAY_MODEL_NAME=Qwen3-8B
export TAPAY_DEVICE_LABEL='AMD Radeon 8060S · ROCm'
if [[ -n "${JUPYTERHUB_SERVICE_PREFIX:-}" ]]; then
  export TAPAY_ROOT_PATH="${JUPYTERHUB_SERVICE_PREFIX%/}/proxy/8000"
fi
if ! curl -fsS http://127.0.0.1:8081/health >/dev/null 2>&1; then
  [[ -x "$LLAMA_SERVER" ]] || { echo 'llama-server not found; see docs/amd-deployment.md' >&2; exit 1; }
  [[ -f "$MODEL_FILE" ]] || { echo 'GGUF model not found; see docs/amd-deployment.md' >&2; exit 1; }
  nohup "$LLAMA_SERVER" -m "$MODEL_FILE" --host 127.0.0.1 --port 8081 \
    --alias Qwen3-8B -ngl 99 -c 32768 -np 1 --jinja --reasoning off \
    --chat-template-kwargs '{"enable_thinking":false}' > logs/model.log 2>&1 &
  echo $! > logs/model.pid
fi
if ! curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
  [[ -x .venv/bin/python ]] || { echo 'Application .venv missing; install dependencies first.' >&2; exit 1; }
  nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > logs/app.log 2>&1 &
  echo $! > logs/app.pid
fi
printf 'Application path: %s/\n' "${TAPAY_ROOT_PATH:-http://127.0.0.1:8000}"
echo 'Wait for model readiness before sending a task.'
