#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$HOST_PYTHON" ]]; then
  echo "python3/python missing in PATH" >&2
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

exec "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/run_pyright_missing_imports_impl.py" "$@"

