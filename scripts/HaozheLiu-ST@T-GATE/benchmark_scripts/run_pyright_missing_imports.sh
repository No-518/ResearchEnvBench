#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (default):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Env selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Resolve python from agent report (default), else python from PATH

Repo selection:
  --repo <path>                  Path to the repository/project to analyze (default: repo root)

Other:
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes pkg)
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mode="system"
repo="$repo_root"
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pyright_extra_args=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

mkdir -p "$repo/$out_dir"
log_txt="$repo/$out_dir/log.txt"
pyright_out="$repo/$out_dir/pyright_output.json"
analysis_json="$repo/$out_dir/analysis.json"
results_json="$repo/$out_dir/results.json"

mkdir -p "$(dirname "$log_txt")"
: >"$log_txt"
exec > >(tee -a "$log_txt") 2>&1

bootstrap_py="$(command -v python3 || command -v python || true)"
if [[ -z "$bootstrap_py" ]]; then
  echo "[pyright] python3/python not found in PATH; cannot write results.json robustly." >&2
  mkdir -p "$repo/$out_dir"
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python3/python not found in PATH"
  },
  "failure_category": "deps",
  "error_excerpt": "python3/python not found in PATH"
}
JSON
  exit 1
fi
status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"
command_str=""
decision_reason=""
pyright_install_attempted=0
pyright_install_command=""
pyright_install_exit=0
pyright_exit=0

cleanup() {
  # Ensure required json files exist.
  [[ -f "$pyright_out" ]] || echo '{}' >"$pyright_out"
  [[ -f "$analysis_json" ]] || echo '{}' >"$analysis_json"

  local git_commit=""
  if command -v git >/dev/null 2>&1; then
    git_commit="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
  fi

  local error_excerpt=""
  error_excerpt="$(
    if [[ -f "$log_txt" ]]; then
      tail -n 220 "$log_txt" || true
    fi
  )"

  if [[ -n "$bootstrap_py" ]]; then
    STAGE_STATUS="$status" STAGE_EXIT_CODE="$exit_code" STAGE_FAILURE_CATEGORY="$failure_category" STAGE_SKIP_REASON="$skip_reason" \
    STAGE_COMMAND="$command_str" STAGE_TIMEOUT_SEC="600" STAGE_PYTHON="$python_bin" STAGE_GIT_COMMIT="$git_commit" \
    STAGE_DECISION_REASON="$decision_reason" PYRIGHT_INSTALL_ATTEMPTED="$pyright_install_attempted" PYRIGHT_INSTALL_COMMAND="$pyright_install_command" \
    PYRIGHT_INSTALL_EXIT="$pyright_install_exit" PYRIGHT_EXIT="$pyright_exit" ERROR_EXCERPT="$error_excerpt" \
    ANALYSIS_PATH="$analysis_json" OUT_RESULTS="$results_json" \
    "$bootstrap_py" - <<'PY' || true
import json
import os

def _as_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

analysis_path = os.environ.get("ANALYSIS_PATH", "")
metrics = {}
missing_packages = []
try:
    if analysis_path and os.path.isfile(analysis_path):
        with open(analysis_path, "r", encoding="utf-8") as f:
            a = json.load(f)
        if isinstance(a, dict):
            m = a.get("metrics")
            if isinstance(m, dict):
                metrics = m
            mp = a.get("missing_packages")
            if isinstance(mp, list):
                missing_packages = mp
except Exception:
    pass

payload = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("STAGE_SKIP_REASON", "unknown"),
    "exit_code": _as_int(os.environ.get("STAGE_EXIT_CODE", "1"), 1),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("STAGE_COMMAND", ""),
    "timeout_sec": _as_int(os.environ.get("STAGE_TIMEOUT_SEC", "600"), 600),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "missing_packages": missing_packages,
    "metrics": metrics,
    "missing_packages_count": metrics.get("missing_packages_count"),
    "total_imported_packages_count": metrics.get("total_imported_packages_count"),
    "missing_package_ratio": metrics.get("missing_package_ratio"),
    "meta": {
        "python": os.environ.get("STAGE_PYTHON", ""),
        "git_commit": os.environ.get("STAGE_GIT_COMMIT", ""),
        "env_vars": {},
        "decision_reason": os.environ.get("STAGE_DECISION_REASON", ""),
        "pyright_install_attempted": bool(_as_int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0"))),
        "pyright_install_command": os.environ.get("PYRIGHT_INSTALL_COMMAND", ""),
        "pyright_install_exit_code": _as_int(os.environ.get("PYRIGHT_INSTALL_EXIT", "0")),
        "pyright_exit_code": _as_int(os.environ.get("PYRIGHT_EXIT", "0")),
    },
    "failure_category": os.environ.get("STAGE_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}

out = os.environ.get("OUT_RESULTS")
if out:
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
PY
  fi
}
trap cleanup EXIT

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"

cd "$repo"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; failure_category="args_unknown"; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; failure_category="args_unknown"; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; failure_category="deps"; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; failure_category="deps"; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      # Default to the agent report python unless explicitly overridden.
      if [[ -n "$bootstrap_py" ]]; then
        if resolved="$("$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" --stage pyright --task check --print-python --report-path "$report_path" --requires-python)"; then
          if [[ -n "$resolved" ]]; then
            py_cmd=("$resolved")
          else
            echo "[pyright] runner resolved empty python path" >&2
            failure_category="missing_report"
            status="failure"
            exit_code=1
            exit 1
          fi
        else
          echo "[pyright] Failed to resolve python from report at $report_path (and --python not provided)" >&2
          failure_category="missing_report"
          status="failure"
          exit_code=1
          exit 1
        fi
      else
        py_cmd=(python)
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      failure_category="args_unknown"
      exit 2
      ;;
  esac
fi

python_bin="${py_cmd[*]}"
echo "[pyright] python_cmd=$python_bin"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] Failed to run python via: ${py_cmd[*]}" >&2
  failure_category="deps"
  status="failure"
  exit_code=1
  exit 1
fi

export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
mkdir -p "$PIP_CACHE_DIR"

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  pyright_install_attempted=1
  pyright_install_command="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] Installing pyright: $pyright_install_command"
  if "${py_cmd[@]}" -m pip install -q pyright; then
    pyright_install_exit=0
  else
    pyright_install_exit=$?
    echo "[pyright] Failed to install pyright (exit=$pyright_install_exit)" >&2
    failure_category="deps"
    status="failure"
    exit_code=1
    exit 1
  fi
fi

# Repo structure detection (priority order).
targets=()
proj_args=()
if [[ -f "pyrightconfig.json" ]]; then
  proj_args=(--project pyrightconfig.json)
  decision_reason="Found pyrightconfig.json; using --project pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && rg -n '^\\[tool\\.pyright\\]' pyproject.toml >/dev/null 2>&1; then
  proj_args=(--project pyproject.toml)
  decision_reason="Found [tool.pyright] in pyproject.toml; using --project pyproject.toml"
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
  decision_reason="Found src/ layout; running pyright on src/ (and tests/ if present)"
else
  mapfile -t pkg_dirs < <(
    find . -maxdepth 4 -type f -name '__init__.py' \
      -not -path './.git/*' \
      -not -path './.venv/*' \
      -not -path './venv/*' \
      -not -path './benchmark_assets/*' \
      -not -path './benchmark_scripts/*' \
      -not -path './build_output/*' \
      -print 2>/dev/null | sed 's|^\\./||' | xargs -r -n1 dirname | sort -u
  )
  for d in "${pkg_dirs[@]}"; do
    [[ -n "$d" ]] && targets+=("$d")
  done
  # Also include top-level .py files (common in small repos).
  for f in ./*.py; do
    [[ -f "$f" ]] && targets+=("${f#./}")
  done
  decision_reason="Detected Python package dirs via __init__.py and top-level .py files"
fi

if [[ ${#proj_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  echo "[pyright] No pyright project/config/src/package dirs found; cannot select targets." >&2
  failure_category="entrypoint_not_found"
  status="failure"
  exit_code=1
  echo '{}' >"$analysis_json"
  echo '{}' >"$pyright_out"
  exit 1
fi

command_str="${py_cmd[*]} -m pyright --level $pyright_level --outputjson ${proj_args[*]} ${targets[*]} ${pyright_extra_args[*]}"
echo "[pyright] command=$command_str"

pyright_exit=0
"${py_cmd[@]}" -m pyright --level "$pyright_level" --outputjson "${proj_args[@]}" "${targets[@]}" "${pyright_extra_args[@]}" >"$pyright_out" || pyright_exit=$?
echo "[pyright] pyright_exit_code=$pyright_exit"

OUT_JSON="$pyright_out" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" MODE="$mode" PY_CMD="${py_cmd[*]}" \
TARGETS="$(printf '%s\n' "${targets[@]}")" DECISION_REASON="$decision_reason" PYRIGHT_EXIT="$pyright_exit" \
"${py_cmd[@]}" - <<'PY' || true
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

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import \"([^.\\\"]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
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
        "benchmark_assets",
        "benchmark_scripts",
        "build_output",
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set[str]:
    pkgs: set[str] = set()
    try:
        src = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py_file))
    except Exception:
        return pkgs
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                pkgs.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                pkgs.add(node.module.split(".")[0])
    return pkgs

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(repo_root):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "targets": [t for t in os.environ.get("TARGETS", "").splitlines() if t.strip()],
        "files_scanned": files_scanned,
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT", "0") or 0),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
results_json.write_text(json.dumps(payload["metrics"], ensure_ascii=False, indent=2), encoding="utf-8")
print(missing_package_ratio)
PY

# Decide final stage status.
if [[ -s "$pyright_out" ]] && [[ -s "$analysis_json" ]]; then
  status="success"
  exit_code=0
  failure_category=""
else
  status="failure"
  exit_code=1
  failure_category="${failure_category:-runtime}"
fi

exit "$exit_code"
