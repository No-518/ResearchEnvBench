#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (default out root: build_output):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection:
  --python <path>                Explicit python executable (highest priority)
  --mode report|venv|uv|conda|poetry|system
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --venv <path>                  For --mode venv/uv
  --conda-env <name>             For --mode conda

Optional:
  --repo <path>                  Repo root (default: parent of this script)
  --out-root <path>              Default: build_output
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright

Examples:
  bash benchmark_scripts/run_pyright_missing_imports.sh
  bash benchmark_scripts/run_pyright_missing_imports.sh --python /opt/scimlopsbench/python
  bash benchmark_scripts/run_pyright_missing_imports.sh --mode venv --venv .venv
EOF
}

mode="report"
repo=""
out_root="build_output"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-root) out_root="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)
      shift
      pyright_extra_args=("$@")
      break
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${repo:-"$(cd "$script_dir/.." && pwd)"}"
report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

stage_dir="$repo/$out_root/pyright"
mkdir -p "$stage_dir"
log="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

# Keep all generated caches under benchmark_assets/cache and prevent __pycache__ in repo.
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export BENCHMARK_ASSETS_DIR="$repo/benchmark_assets"
export XDG_CACHE_HOME="$BENCHMARK_ASSETS_DIR/cache/xdg"
export PIP_CACHE_DIR="$BENCHMARK_ASSETS_DIR/cache/pip"
export HF_HOME="$BENCHMARK_ASSETS_DIR/cache/huggingface"
export TRANSFORMERS_CACHE="$BENCHMARK_ASSETS_DIR/cache/huggingface/transformers"
export HF_DATASETS_CACHE="$BENCHMARK_ASSETS_DIR/cache/huggingface/datasets"
export TORCH_HOME="$BENCHMARK_ASSETS_DIR/cache/torch"
export SENTENCE_TRANSFORMERS_HOME="$BENCHMARK_ASSETS_DIR/cache/sentence_transformers"
export TMPDIR="$BENCHMARK_ASSETS_DIR/cache/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
mkdir -p "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" "$HF_HOME" "$TRANSFORMERS_CACHE" "$HF_DATASETS_CACHE" "$TORCH_HOME" "$SENTENCE_TRANSFORMERS_HOME" "$TMPDIR"

status="failure"
skip_reason="not_applicable"
exit_code=1
failure_category="unknown"
decision_reason=""
pyright_cmd_str=""
pyright_install_attempted=0
pyright_install_command=""
pyright_exit_code=0

echo "[pyright] repo=$repo" >"$log"
{
  echo "[pyright] stage_dir=$stage_dir"
  echo "[pyright] report_path=$report_path"
  echo "[pyright] mode=$mode"
} >>"$log"

write_stub_json() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf '%s\n' '{}' >"$path"
  fi
}

finalize() {
  local rc="$1"
  if [[ "$status" != "skipped" ]]; then
    if [[ "$rc" -eq 0 && "$failure_category" == "unknown" ]]; then
      status="success"
      exit_code=0
    elif [[ "$rc" -eq 0 && "$status" == "failure" ]]; then
      # status was set to failure explicitly
      exit_code=1
    else
      status="${status:-failure}"
      exit_code=1
    fi
  else
    exit_code=0
  fi

  write_stub_json "$out_json"
  write_stub_json "$analysis_json"

  REPO_ROOT="$repo" LOG_PATH="$log" RESULTS_PATH="$results_json" ANALYSIS_PATH="$analysis_json" \
  STAGE_STATUS="$status" STAGE_SKIP_REASON="$skip_reason" STAGE_EXIT_CODE="$exit_code" \
  PYRIGHT_COMMAND="$pyright_cmd_str" PYTHON_BIN="$python_bin" DECISION_REASON="$decision_reason" \
  INSTALL_ATTEMPTED="$pyright_install_attempted" INSTALL_COMMAND="$pyright_install_command" \
  PYRIGHT_EXIT_CODE="$pyright_exit_code" FAILURE_CATEGORY="$failure_category" \
  python - <<'PY'
import json
import os
import pathlib
import subprocess
import time

repo_root = pathlib.Path(os.environ["REPO_ROOT"])
log_path = pathlib.Path(os.environ["LOG_PATH"])
results_path = pathlib.Path(os.environ["RESULTS_PATH"])
analysis_path = pathlib.Path(os.environ["ANALYSIS_PATH"])

def tail(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

git_commit = ""
try:
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
except Exception:
    git_commit = ""

# Try to include metrics from analysis.json if present.
metrics = {}
missing_packages = []
try:
    a = json.loads(analysis_path.read_text(encoding="utf-8"))
    if isinstance(a, dict):
        metrics = a.get("metrics", {}) if isinstance(a.get("metrics"), dict) else {}
        missing_packages = a.get("missing_packages", []) if isinstance(a.get("missing_packages"), list) else []
except Exception:
    pass

assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

results = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("STAGE_SKIP_REASON", "not_applicable"),
    "exit_code": int(os.environ.get("STAGE_EXIT_CODE", "1")),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_COMMAND", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": assets,
    "missing_packages": missing_packages,
    "metrics": metrics,
    "meta": {
        "python": os.environ.get("PYTHON_BIN", ""),
        "git_commit": git_commit,
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "TRANSFORMERS_CACHE",
                "HF_DATASETS_CACHE",
                "PIP_CACHE_DIR",
                "XDG_CACHE_HOME",
                "SENTENCE_TRANSFORMERS_HOME",
                "TORCH_HOME",
                "PYTHONDONTWRITEBYTECODE",
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
            ]
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "pyright_install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0")),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(log_path),
}

results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

trap 'rc=$?; finalize "$rc"; exit "$exit_code"' EXIT

resolve_report_python() {
  REPORT_PATH="$report_path" python - <<'PY'
import json
import os
import pathlib
import sys

rp = pathlib.Path(os.environ["REPORT_PATH"])
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
except Exception as e:
    print(f"ERROR: invalid report json: {e}", file=sys.stderr)
    sys.exit(1)
py = data.get("python_path")
if not isinstance(py, str) or not py.strip():
    print("ERROR: report missing python_path", file=sys.stderr)
    sys.exit(1)
print(py)
PY
}

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    report)
      python_bin="$(resolve_report_python)" || { failure_category="missing_report"; echo "[pyright] missing/invalid report" >>"$log"; exit 1; }
      py_cmd=("$python_bin")
      ;;
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >>"$log"; failure_category="args_unknown"; exit 1; }
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >>"$log"; failure_category="args_unknown"; exit 1; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >>"$log"; failure_category="deps"; exit 1; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >>"$log"; failure_category="deps"; exit 1; }
      py_cmd=(poetry run python)
      ;;
    system)
      command -v python >/dev/null 2>&1 || { echo "python not found in PATH" >>"$log"; failure_category="deps"; exit 1; }
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >>"$log"
      failure_category="args_unknown"
      exit 1
      ;;
  esac
fi

echo "[pyright] python_cmd=${py_cmd[*]}" >>"$log"
if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log" 2>&1; then
  failure_category="deps"
  exit 1
fi

cd "$repo"

# Install Pyright into the selected environment if missing.
if ! "${py_cmd[@]}" -c 'import pyright' >>"$log" 2>&1; then
  pyright_install_attempted=1
  pyright_install_command="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] installing pyright via: $pyright_install_command" >>"$log"
  if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log" 2>&1; then
    # Heuristic categorization
    if rg -n "Temporary failure|Name or service not known|Connection.*failed|timed out|No route to host" "$log" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    exit 1
  fi
fi

# Determine how to target Pyright.
project_args=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project "pyrightconfig.json")
  decision_reason="Found pyrightconfig.json; running Pyright with --project pyrightconfig.json."
elif [[ -f "pyproject.toml" ]]; then
  # Detect [tool.pyright] via stdlib tomllib/tomli
  if "${py_cmd[@]}" - <<'PY' >>"$log" 2>&1; then
import pathlib, sys
p = pathlib.Path("pyproject.toml")
txt = p.read_text(encoding="utf-8", errors="ignore")
sys.exit(0 if "[tool.pyright]" in txt else 1)
PY
    project_args=(--project "pyproject.toml")
    decision_reason="Found pyproject.toml with [tool.pyright]; running Pyright with --project pyproject.toml."
  fi
fi

if [[ ${#project_args[@]} -eq 0 ]]; then
  if [[ -d "src" ]]; then
    targets=("src")
    [[ -d "tests" ]] && targets+=("tests")
    decision_reason="No Pyright project config; detected src/ layout; targeting src (and tests if present)."
  else
    # Detect package directories (contain __init__.py) near repo root.
    mapfile -t targets < <(
      find . -maxdepth 3 -type f -name "__init__.py" \
        -not -path "./.git/*" \
        -not -path "./.venv/*" \
        -not -path "./venv/*" \
        -not -path "./build_output/*" \
        -not -path "./benchmark_assets/*" \
        -print \
        | awk -F/ '{print $2}' \
        | sort -u
    )
    if [[ ${#targets[@]} -gt 0 ]]; then
      decision_reason="No Pyright project config; detected package dirs via __init__.py; targeting: ${targets[*]}."
    else
      echo "[pyright] ERROR: could not determine Pyright targets (no config, no src/, no packages)" >>"$log"
      failure_category="entrypoint_not_found"
      exit 1
    fi
  fi
fi

pyright_cmd=("${py_cmd[@]}" -m pyright)
if [[ ${#project_args[@]} -gt 0 ]]; then
  pyright_cmd+=("${project_args[@]}")
else
  pyright_cmd+=("${targets[@]}")
fi
pyright_cmd+=(--level "$pyright_level" --outputjson)
if [[ ${#pyright_extra_args[@]} -gt 0 ]]; then
  pyright_cmd+=("${pyright_extra_args[@]}")
fi

pyright_cmd_str="${pyright_cmd[*]}"
echo "[pyright] command=$pyright_cmd_str" >>"$log"

set +e
"${pyright_cmd[@]}" >"$out_json" 2>>"$log"
pyright_exit_code=$?
set -e
echo "[pyright] pyright_exit_code=$pyright_exit_code (ignored for stage success)" >>"$log"

if [[ ! -s "$out_json" ]]; then
  echo "{}" >"$out_json"
fi

# Analyze output: only reportMissingImports diagnostics.
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" TARGETS="${targets[*]:-}" MODE="$mode" PY_CMD="${py_cmd[*]}" \
  "${py_cmd[@]}" - <<'PY' >>"$log" 2>&1
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

repo_root = pathlib.Path(".").resolve()
targets_env = os.environ.get("TARGETS", "").strip()
targets = [t for t in targets_env.split() if t]
scan_roots = [repo_root / t for t in targets] if targets else [repo_root]

data: dict = {}
parse_error: str = ""
try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    parse_error = str(e)
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^"]+)"')
missing_packages = sorted(
    {m.group(1).split(".")[0] for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

def iter_py_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    exclude_dirs = {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".venv",
        "venv",
        "build",
        "dist",
        "node_modules",
        "build_output",
        "benchmark_assets",
    }
    if not root.exists():
        return
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set[str]:
    pkgs: set[str] = set()
    try:
        src = py_file.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src, filename=str(py_file))
    except Exception:
        return pkgs
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                pkgs.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0) == 0 and getattr(node, "module", None):
                pkgs.add(node.module.split(".")[0])  # type: ignore[union-attr]
    return pkgs

all_imported_packages: set[str] = set()
files_scanned = 0
for root in scan_roots:
    for py_file in iter_py_files(root):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}" if total_imported_packages_count else "0/0"

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "targets": targets,
        "files_scanned": files_scanned,
        "pyright_json_parse_error": parse_error,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
results_json.write_text(json.dumps(payload["metrics"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

status="success"
exit_code=0
failure_category="unknown"
