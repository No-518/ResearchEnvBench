#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (reportMissingImports).

Outputs:
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository/project to analyze

Python selection (pick ONE; default: auto -> SCIMLOPSBENCH_PYTHON -> report.json python_path):
  --python <path>                Explicit python executable to use (highest)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Report path override (auto mode only):
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json

Optional:
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonversion 3.10)

Notes:
  - Pyright is installed (via pip) into the selected interpreter if missing.
  - Non-zero Pyright exit codes do not fail this stage unless output JSON is missing/invalid.
EOF
}

mode="auto"
repo=""
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

repo="$(cd "$repo" && pwd)"
out_dir="$repo/build_output/pyright"
mkdir -p "$out_dir"

log_file="$out_dir/log.txt"
pyright_out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_file"
exec > >(tee -a "$log_file") 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"

status="failure"
failure_category="unknown"
exit_code=1
skip_reason="unknown"
decision_reason=""
install_attempted=0
install_cmd=""
pyright_exit_code=0
pyright_cmd_str=""

write_stub_json() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf '%s\n' '{}' >"$path"
  fi
}

finalize() {
  write_stub_json "$pyright_out_json"
  write_stub_json "$analysis_json"
  local pybin
  pybin="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"
  "$pybin" - <<'PY' "$results_json" "$analysis_json" "$log_file"
import os
import json, pathlib, sys

results_path = pathlib.Path(sys.argv[1])
analysis_path = pathlib.Path(sys.argv[2])
log_path = pathlib.Path(sys.argv[3])

analysis = {}
try:
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
except Exception:
    analysis = {}

metrics = analysis.get("metrics") if isinstance(analysis, dict) else {}
if not isinstance(metrics, dict):
    metrics = {}

try:
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]
    excerpt = "\n".join(tail)
except Exception:
    excerpt = ""

payload = {
    "status": str(os.environ.get("PYRIGHT_STATUS", "failure")),
    "skip_reason": str(os.environ.get("PYRIGHT_SKIP_REASON", "unknown")),
    "exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "1")),
    "stage": "pyright",
    "task": "check",
    "command": str(os.environ.get("PYRIGHT_COMMAND", "")),
    "timeout_sec": int(os.environ.get("PYRIGHT_TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": str(os.environ.get("PYRIGHT_PYTHON_EXE", "")),
        "python_version": str(os.environ.get("PYRIGHT_PYTHON_VER", "")),
        "git_commit": str(os.environ.get("PYRIGHT_GIT_COMMIT", "")),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": str(os.environ.get("PYRIGHT_DECISION_REASON", "")),
        "pyright": {
            "level": str(os.environ.get("PYRIGHT_LEVEL", "")),
            "project": str(os.environ.get("PYRIGHT_PROJECT", "")),
            "targets": json.loads(os.environ.get("PYRIGHT_TARGETS_JSON", "[]")),
            "exit_code": int(os.environ.get("PYRIGHT_TOOL_EXIT", "0")),
            "install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0"))),
            "install_cmd": str(os.environ.get("PYRIGHT_INSTALL_CMD", "")),
            "python_source": str(os.environ.get("PYRIGHT_PY_SOURCE", "")),
            "python_warning": str(os.environ.get("PYRIGHT_PY_WARN", "")),
        },
    },
    "failure_category": str(os.environ.get("PYRIGHT_FAILURE_CATEGORY", "unknown")),
    "error_excerpt": excerpt,
}

for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
    if k in metrics:
        payload[k] = metrics[k]

results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

trap 'finalize' EXIT

py_cmd=()
python_source=""
python_warning=""

resolve_python_from_report() {
  local rp="$1"
  local py
  local pybin
  pybin="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"
  py="$("$pybin" - "$rp" <<'PY' 2>/dev/null || true
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
print(data.get("python_path", "") or "")
PY
)"
  echo "$py"
}

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_source="cli"
else
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      python_source="mode:venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_source="mode:uv"
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_source="mode:conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_source="mode:poetry"
      ;;
    system)
      py_cmd=(python)
      python_source="mode:system"
      ;;
    auto)
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
        python_source="env:SCIMLOPSBENCH_PYTHON"
      else
        rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
        if [[ -f "$rp" ]]; then
          resolved="$(resolve_python_from_report "$rp")"
          if [[ -n "$resolved" ]]; then
            py_cmd=("$resolved")
            python_source="report:python_path"
          else
            echo "[pyright] ERROR: python_path missing in report at $rp (set --python or SCIMLOPSBENCH_PYTHON)" >&2
            python_source="missing_report"
            python_warning="python_path missing in report"
            status="failure"
            failure_category="missing_report"
            exit_code=1
            export PYRIGHT_STATUS="$status" PYRIGHT_FAILURE_CATEGORY="$failure_category" PYRIGHT_EXIT_CODE="$exit_code" PYRIGHT_DECISION_REASON="Report missing python_path; cannot resolve interpreter."
            exit 1
          fi
        else
          echo "[pyright] ERROR: report missing at $rp (set --python or SCIMLOPSBENCH_PYTHON)" >&2
          python_source="missing_report"
          python_warning="report missing"
          status="failure"
          failure_category="missing_report"
          exit_code=1
          export PYRIGHT_STATUS="$status" PYRIGHT_FAILURE_CATEGORY="$failure_category" PYRIGHT_EXIT_CODE="$exit_code" PYRIGHT_DECISION_REASON="Report missing; cannot resolve interpreter."
          exit 1
        fi
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

cd "$repo"

python_exe="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_ver="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
echo "[pyright] python_source=$python_source"
echo "[pyright] python_exe=$python_exe"
echo "[pyright] python_ver=$python_ver"
if [[ -n "$python_warning" ]]; then
  echo "[pyright] python_warning=$python_warning"
fi

# -----------------
# Target selection
# -----------------
project_arg=()
project_file=""
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  project_file="pyrightconfig.json"
  project_arg=(--project "$project_file")
  decision_reason="Using pyrightconfig.json (highest priority)."
elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_file="pyproject.toml"
  project_arg=(--project "$project_file")
  decision_reason="Using pyproject.toml with [tool.pyright] (2nd priority)."
elif [[ -d "src" ]]; then
  targets=(src)
  [[ -d "tests" ]] && targets+=(tests)
  decision_reason="No pyright config found; using src/ layout targets."
else
  mapfile -t pkg_dirs < <(
    find . -maxdepth 2 -type f -name "__init__.py" \
      -not -path "./.git/*" \
      -not -path "./.venv/*" \
      -not -path "./venv/*" \
      -not -path "./build_output/*" \
      -print \
      | sed -E 's|^\\./||' \
      | awk -F/ '{print $1}' \
      | sort -u
  )
  if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="No pyright config found; using detected package dirs containing __init__.py."
  else
    echo "[pyright] ERROR: no pyrightconfig/pyproject/src/ or package dirs detected" >&2
    status="failure"
    failure_category="entrypoint_not_found"
    exit_code=1
    skip_reason="unknown"
    decision_reason="Failed to detect any Python targets for Pyright per required priority order."
    printf '%s\n' '{}' >"$pyright_out_json"
    printf '%s\n' '{}' >"$analysis_json"
    export PYRIGHT_STATUS="$status" PYRIGHT_FAILURE_CATEGORY="$failure_category" PYRIGHT_EXIT_CODE="$exit_code" PYRIGHT_DECISION_REASON="$decision_reason"
    exit 1
  fi
fi

echo "[pyright] targets: ${targets[*]:-(project-defined)}"
echo "[pyright] project: ${project_file:-none}"
echo "[pyright] decision_reason: $decision_reason"

# -----------------
# Ensure pyright installed
# -----------------
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="$python_exe -m pip install -q pyright"
  echo "[pyright] pyright missing; installing: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip --version >/dev/null 2>&1
  pip_ok=$?
  set -e
  if [[ "$pip_ok" -ne 0 ]]; then
    echo "[pyright] ERROR: pip not available in selected python environment" >&2
    status="failure"
    failure_category="deps"
    exit_code=1
    printf '%s\n' '{}' >"$pyright_out_json"
    printf '%s\n' '{}' >"$analysis_json"
    export PYRIGHT_STATUS="$status" PYRIGHT_FAILURE_CATEGORY="$failure_category" PYRIGHT_EXIT_CODE="$exit_code" PYRIGHT_DECISION_REASON="$decision_reason"
    exit 1
  fi

  set +e
  install_output="$("${py_cmd[@]}" -m pip install -q pyright 2>&1)"
  install_rc=$?
  set -e
  if [[ "$install_rc" -ne 0 ]]; then
    echo "$install_output" >&2
    if echo "$install_output" | rg -i "(temporary failure|name resolution|connection|timed out|proxy|ssl)" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    status="failure"
    exit_code=1
    printf '%s\n' '{}' >"$pyright_out_json"
    printf '%s\n' '{}' >"$analysis_json"
    export PYRIGHT_STATUS="$status" PYRIGHT_FAILURE_CATEGORY="$failure_category" PYRIGHT_EXIT_CODE="$exit_code" PYRIGHT_DECISION_REASON="$decision_reason"
    exit 1
  fi
fi

# -----------------
# Run pyright
# -----------------
pyright_cmd=( "${py_cmd[@]}" -m pyright )
if [[ "${#targets[@]}" -gt 0 ]]; then
  pyright_cmd+=( "${targets[@]}" )
fi
pyright_cmd+=( --level "$pyright_level" --outputjson )
if [[ "${#project_arg[@]}" -gt 0 ]]; then
  pyright_cmd+=( "${project_arg[@]}" )
fi
if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
  pyright_cmd+=( "${pyright_extra_args[@]}" )
fi

pyright_cmd_str="$(printf '%q ' "${pyright_cmd[@]}")"
echo "[pyright] command: $pyright_cmd_str"

set +e
"${pyright_cmd[@]}" >"$pyright_out_json"
pyright_exit_code=$?
set -e
echo "[pyright] pyright_exit_code=$pyright_exit_code (non-zero does not fail stage by itself)"

# -----------------
# Analyze output
# -----------------
set +e
"${py_cmd[@]}" - <<'PY' "$pyright_out_json" "$analysis_json" "$python_source" "$python_warning"
import ast
import json
import os
import pathlib
import re
import sys
from typing import Iterable

out_json = pathlib.Path(sys.argv[1])
analysis_json = pathlib.Path(sys.argv[2])
python_source = sys.argv[3]
python_warning = sys.argv[4]

repo_root = pathlib.Path(".").resolve()

data = {}
try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", [])
if not isinstance(diagnostics, list):
    diagnostics = []

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


def collect_imported_packages(py_file: pathlib.Path) -> set[str]:
    pkgs: set[str] = set()
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


all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(repo_root):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}" if total_imported_packages_count else "0/0"

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "python_source": python_source,
        "python_warning": python_warning,
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
analyze_rc=$?
set -e

if [[ "$analyze_rc" -ne 0 ]]; then
  echo "[pyright] ERROR: failed to analyze pyright output" >&2
  status="failure"
  failure_category="invalid_json"
  exit_code=1
else
  status="success"
  failure_category="unknown"
  exit_code=0
fi

export PYRIGHT_STATUS="$status"
export PYRIGHT_SKIP_REASON="$skip_reason"
export PYRIGHT_EXIT_CODE="$exit_code"
export PYRIGHT_FAILURE_CATEGORY="$failure_category"
export PYRIGHT_DECISION_REASON="$decision_reason"
export PYRIGHT_COMMAND="$pyright_cmd_str"
export PYRIGHT_TIMEOUT_SEC="600"
export PYRIGHT_PYTHON_EXE="$python_exe"
export PYRIGHT_PYTHON_VER="$python_ver"
export PYRIGHT_GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
export PYRIGHT_LEVEL="$pyright_level"
export PYRIGHT_PROJECT="$project_file"
{
  pybin="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"
  if [[ "${#targets[@]}" -gt 0 ]]; then
    PYRIGHT_TARGETS_JSON="$("$pybin" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${targets[@]}")"
  else
    PYRIGHT_TARGETS_JSON="[]"
  fi
  export PYRIGHT_TARGETS_JSON
}
export PYRIGHT_TOOL_EXIT="$pyright_exit_code"
export PYRIGHT_INSTALL_ATTEMPTED="$install_attempted"
export PYRIGHT_INSTALL_CMD="$install_cmd"
export PYRIGHT_PY_SOURCE="$python_source"
export PYRIGHT_PY_WARN="$python_warning"

if [[ "$status" == "success" ]]; then
  exit 0
fi
exit 1
