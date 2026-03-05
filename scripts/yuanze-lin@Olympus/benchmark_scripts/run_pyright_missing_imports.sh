#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (default):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository/project to analyze

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report (default) or PATH

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --report-path <path>           Report JSON path (default: /opt/scimlopsbench/report.json)
  -- <pyright args...>           Extra args passed to Pyright

Notes:
  - Only diagnostics with rule == "reportMissingImports" are counted.
  - If Pyright isn't importable, this script attempts:
      "<resolved_python>" -m pip install -q pyright
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
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
    --out-dir) out_dir="${2:-}"; shift 2 ;;
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
      exit 2
      ;;
  esac
done

if [[ -z "$repo" ]]; then
  echo "--repo is required" >&2
  usage
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

cd "$repo"
mkdir -p "$out_dir"

log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

# Keep any pip cache within the allowed benchmark_assets/ tree.
mkdir -p "./benchmark_assets/cache/pip_cache" || true
export PIP_CACHE_DIR="$(pwd)/benchmark_assets/cache/pip_cache"
export XDG_CACHE_HOME="$(pwd)/benchmark_assets/cache/xdg_cache"
export XDG_CONFIG_HOME="$(pwd)/benchmark_assets/cache/xdg_config"
export XDG_DATA_HOME="$(pwd)/benchmark_assets/cache/xdg_data"
mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" || true
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$(pwd)/build_output/pyright/pycache"
mkdir -p "$PYTHONPYCACHEPREFIX" || true

: > "$log_file"
exec > >(tee -a "$log_file") 2>&1

status="success"
exit_code=0
failure_category="unknown"
skip_reason="not_applicable"
decision_reason=""
command_str=""

py_cmd=()
python_resolution="unknown"
python_warning=""
install_attempted=0
install_cmd=""

_fallback_python_for_parsing() {
  if command -v python3 >/dev/null 2>&1; then echo "python3"; return 0; fi
  if command -v python >/dev/null 2>&1; then echo "python"; return 0; fi
  return 1
}

_resolve_python_from_report() {
  local parser_py=""
  parser_py="$(_fallback_python_for_parsing)" || return 1
  local rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
  if [[ ! -f "$rp" ]]; then
    echo ""
    return 2
  fi
  "$parser_py" - <<'PY' "$rp"
import json, sys
from pathlib import Path
rp = Path(sys.argv[1])
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
except Exception:
    sys.exit(3)
py = data.get("python_path")
if not py:
    sys.exit(4)
print(py)
PY
}

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("${SCIMLOPSBENCH_PYTHON}")
  python_resolution="env"
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      python_resolution="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution="uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_resolution="conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_resolution="poetry"
      ;;
    system)
      # Default to the python specified by the agent report, if present.
      resolved="$(_resolve_python_from_report)"
      rc=$?
      if [[ $rc -eq 0 && -n "$resolved" ]]; then
        py_cmd=("$resolved")
        python_resolution="report"
      elif [[ $rc -eq 4 ]]; then
        python_warning="report_missing_python_path"
        py_cmd=(python)
        python_resolution="path"
      else
        echo "[pyright] missing/invalid report; pass --python or set SCIMLOPSBENCH_PYTHON/SCIMLOPSBENCH_REPORT" >&2
        python_warning="missing_report"
        status="failure"
        exit_code=1
        failure_category="missing_report"
        decision_reason="report missing/invalid and no --python override provided"
        py_cmd=(python)
        python_resolution="path"
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"
echo "[pyright] python_cmd=${py_cmd[*]}"
echo "[pyright] python_resolution=$python_resolution"

if [[ "$status" == "success" ]]; then
  if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    echo "[pyright] Failed to run python via: ${py_cmd[*]}" >&2
    status="failure"
    exit_code=1
    failure_category="deps"
    decision_reason="python interpreter could not be executed"
  else
    # Ensure pyright is available, installing if needed.
    if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
      install_attempted=1
      install_cmd="${py_cmd[*]} -m pip install -q pyright"
      echo "[pyright] Installing pyright: $install_cmd"
      pip_out="$("${py_cmd[@]}" -m pip install -q pyright 2>&1)" || true
      echo "$pip_out"
      if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
        echo "[pyright] Failed to install pyright" >&2
        status="failure"
        exit_code=1
        if echo "$pip_out" | grep -E -q "Failed to establish a new connection|Name or service not known|Temporary failure in name resolution|No matching distribution found|Could not find a version"; then
          failure_category="download_failed"
        else
          failure_category="deps"
        fi
        decision_reason="pyright missing and installation failed"
      fi
    fi
  fi
fi

# Determine pyright targets/project.
pyright_args=()
targets=()
if [[ "$status" == "success" ]]; then
  if [[ -f "pyrightconfig.json" ]]; then
    pyright_args+=(--project pyrightconfig.json)
    decision_reason="pyrightconfig.json detected"
  elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
    pyright_args+=(--project pyproject.toml)
    decision_reason="pyproject.toml with [tool.pyright] detected"
  elif [[ -d "src" ]]; then
    targets+=(src)
    [[ -d "tests" ]] && targets+=(tests)
    decision_reason="src/ layout detected"
  else
    mapfile -t pkgs < <(find . -type f -name '__init__.py' \
      -not -path './.git/*' \
      -not -path './.venv/*' \
      -not -path './venv/*' \
      -not -path './build_output/*' \
      -not -path './benchmark_assets/*' \
      -not -path './benchmark_scripts/*' \
      -print | sed -E 's#^\\./##' | awk -F/ 'NF>=2{print $1}' | sort -u)
    if [[ ${#pkgs[@]} -gt 0 ]]; then
      targets+=("${pkgs[@]}")
      decision_reason="package dirs with __init__.py detected: ${targets[*]}"
    else
      echo "[pyright] No pyright config, src/, or package dirs found." >&2
      status="failure"
      exit_code=1
      failure_category="entrypoint_not_found"
      decision_reason="no pyright target could be determined"
    fi
  fi
fi

if [[ "$status" == "success" ]]; then
  command_str="${py_cmd[*]} -m pyright ${pyright_args[*]} ${targets[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
  echo "[pyright] Running: $command_str"
  # Always produce JSON output even if Pyright reports issues (non-zero exit code).
  if ! "${py_cmd[@]}" -m pyright "${pyright_args[@]}" "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json"; then
    echo "[pyright] Pyright returned non-zero (continuing)." >&2
  fi
fi

# Ensure output files exist (even on failure).
if [[ ! -f "$out_json" ]]; then
  echo "{\"error\":\"pyright output missing\"}" > "$out_json"
fi

git_commit="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
python_path_print="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_version_print="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

writer_py="$(_fallback_python_for_parsing)" || writer_py="${py_cmd[0]}"

PYRIGHT_OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
STAGE_STATUS="$status" STAGE_EXIT_CODE="$exit_code" STAGE_FAILURE_CATEGORY="$failure_category" STAGE_SKIP_REASON="$skip_reason" \
TIMEOUT_SEC="600" PYRIGHT_LEVEL="$pyright_level" PYRIGHT_CMD="$command_str" \
MODE="$mode" PYTHON_CMD="${py_cmd[*]}" PYTHON_RESOLUTION="$python_resolution" PYTHON_WARNING="$python_warning" \
PYRIGHT_INSTALL_ATTEMPTED="$install_attempted" PYRIGHT_INSTALL_CMD="$install_cmd" \
GIT_COMMIT="$git_commit" PYTHON_PATH="$python_path_print" PYTHON_VERSION="$python_version_print" \
DECISION_REASON="$decision_reason" LOG_FILE="$log_file" \
  "$writer_py" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Dict, Iterable, List, Set, Tuple

def tail_lines(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

repo_root = pathlib.Path(".").resolve()

out_json = pathlib.Path(os.environ["PYRIGHT_OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_file = pathlib.Path(os.environ.get("LOG_FILE", ""))

status = os.environ.get("STAGE_STATUS", "failure")
exit_code = int(os.environ.get("STAGE_EXIT_CODE", "1"))
failure_category = os.environ.get("STAGE_FAILURE_CATEGORY", "unknown")
skip_reason = os.environ.get("STAGE_SKIP_REASON", "unknown")

command = os.environ.get("PYRIGHT_CMD", "")
timeout_sec = int(os.environ.get("TIMEOUT_SEC", "600"))

pyright_level = os.environ.get("PYRIGHT_LEVEL", "error")

raw_data = {}
try:
    raw_data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    raw_data = {"error": "invalid pyright output json"}
    status = "failure"
    exit_code = 1
    if failure_category == "unknown":
        failure_category = "invalid_json"

diagnostics = raw_data.get("generalDiagnostics", []) if isinstance(raw_data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
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
        "build_output",
        "benchmark_assets",
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> Set[str]:
    pkgs: Set[str] = set()
    try:
        src = py_file.read_text(encoding="utf-8", errors="replace")
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

all_imported_packages: Set[str] = set()
files_scanned = 0
for py_file in iter_py_files(repo_root):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = (
    f"{missing_packages_count}/{total_imported_packages_count}"
    if total_imported_packages_count
    else "0/0"
)

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": raw_data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PYTHON_CMD", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
        "python_warning": os.environ.get("PYTHON_WARNING", ""),
        "pyright_level": pyright_level,
        "pyright_install_attempted": os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0") == "1",
        "pyright_install_cmd": os.environ.get("PYRIGHT_INSTALL_CMD", ""),
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(
    json.dumps(analysis_payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

results_payload: Dict[str, object] = {
    "status": status,
    "skip_reason": skip_reason,
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": command,
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "metrics": analysis_payload["metrics"],
    "meta": {
        "python": os.environ.get("PYTHON_PATH", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": analysis_payload["meta"]["pyright_install_attempted"],
        "pyright_install_cmd": analysis_payload["meta"]["pyright_install_cmd"],
    },
    "failure_category": failure_category,
    "error_excerpt": tail_lines(log_file) if status == "failure" and log_file else "",
  }

results_json.write_text(
    json.dumps(results_payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(missing_package_ratio)
PY

exit "$exit_code"
