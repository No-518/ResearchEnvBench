#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule == reportMissingImports).

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository root to analyze

Python environment selection (priority):
  1) --python <path>                          Explicit python executable
  2) Env var SCIMLOPSBENCH_PYTHON             Explicit python executable
  3) /opt/scimlopsbench/report.json python_path (or SCIMLOPSBENCH_REPORT)
  4) --mode <venv|uv|conda|poetry|system>     Manual selection

Modes:
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonversion 3.10)
EOF
}

mode=""
repo=""
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

if [[ -z "$repo" ]]; then
  echo "--repo is required" >&2
  usage
  exit 2
fi

mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: > "$log_file"
exec > >(tee -a "$log_file") 2>&1

cd "$repo"

echo '{}' > "$out_json"

json_py="$(command -v python3 || command -v python || true)"
if [[ -z "$json_py" ]]; then
  echo "python not found on PATH; cannot write JSON outputs." >&2
  exit 1
fi

py_cmd=()
python_resolution="unknown"
python_resolution_warning=""

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("${SCIMLOPSBENCH_PYTHON}")
  python_resolution="env"
else
  # Prefer report.json python_path if available.
  if [[ -f "$report_path" ]]; then
    report_python="$("$json_py" - <<PY || true
import json, sys
from pathlib import Path
p = Path(r"""$report_path""")
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(2)
py = data.get("python_path")
if isinstance(py, str) and py.strip():
  print(py.strip())
PY
)"
    if [[ -n "$report_python" ]]; then
      py_cmd=("$report_python")
      python_resolution="report"
    fi
  fi

  if [[ ${#py_cmd[@]} -eq 0 ]]; then
    case "${mode:-system}" in
      venv)
        [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
        py_cmd=("$venv_dir/bin/python"); python_resolution="venv" ;;
      uv)
        venv_dir="${venv_dir:-.venv}"
        py_cmd=("$venv_dir/bin/python"); python_resolution="uv" ;;
      conda)
        [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
        command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
        py_cmd=(conda run -n "$conda_env" python); python_resolution="conda" ;;
      poetry)
        command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
        py_cmd=(poetry run python); python_resolution="poetry" ;;
      system|"")
        py_cmd=(python); python_resolution="system" ;;
      *)
        echo "Unknown --mode: $mode" >&2
        exit 2 ;;
    esac
  fi
fi

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"
install_attempted=0
install_cmd=""
pyright_rc=""
pyright_cmd_str=""
decision_reason=""

git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
python_exe="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_ver="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

if [[ -z "$python_exe" ]]; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  failure_category="deps"
  decision_reason="Failed to execute selected python interpreter."
else
  # Ensure pyright is importable; attempt install if missing (mandatory).
  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
    install_attempted=1
    install_cmd="${py_cmd[*]} -m pip install -q pyright"
    echo "[pyright] Installing pyright into selected environment: $install_cmd"
    set +e
    "${py_cmd[@]}" -m pip install -q pyright
    pip_rc=$?
    set -e
    if [[ $pip_rc -ne 0 ]]; then
      failure_category="deps"
      if tail -n 200 "$log_file" | rg -n -S "(Temporary failure in name resolution|ConnectionError|Network is unreachable|Could not fetch URL|Read timed out|ProxyError|SSLError)" >/dev/null 2>&1; then
        failure_category="download_failed"
      fi
      decision_reason="Pyright was missing and installation failed."
    fi
  fi

  if [[ "$failure_category" == "unknown" ]]; then
    # Determine targets/project.
    project_args=()
    targets=()
    if [[ -f "pyrightconfig.json" ]]; then
      project_args=(--project pyrightconfig.json)
      decision_reason="Found pyrightconfig.json; running pyright with --project pyrightconfig.json."
    elif [[ -f "pyproject.toml" ]] && rg -n "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml >/dev/null 2>&1; then
      project_args=(--project pyproject.toml)
      decision_reason="Found [tool.pyright] in pyproject.toml; running pyright with --project pyproject.toml."
    elif [[ -d "src" ]]; then
      targets+=(src)
      [[ -d "tests" ]] && targets+=(tests)
      decision_reason="Detected src/ layout; running pyright on src (and tests if present)."
    else
      mapfile -t detected < <("$json_py" - <<'PY'
import pathlib

root = pathlib.Path(".").resolve()
exclude = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "node_modules",
    "build_output",
    "benchmark_assets",
    "benchmark_scripts",
}

pkgs = set()
for init_py in root.rglob("__init__.py"):
    parts = init_py.relative_to(root).parts
    if any(p in exclude for p in parts):
        continue
    if len(parts) >= 2:
        pkgs.add(parts[0])

for p in sorted(pkgs):
    print(p)
PY
)
      if [[ ${#detected[@]} -gt 0 ]]; then
        targets+=("${detected[@]}")
        [[ -d "tests" ]] && targets+=(tests)
        decision_reason="Detected package dirs with __init__.py; running pyright on: ${targets[*]}"
      fi
    fi

    if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
      failure_category="entrypoint_not_found"
      decision_reason="Could not determine any valid pyright targets (no pyrightconfig.json, no [tool.pyright], no src/, and no package dirs)."
    else
      # Run pyright; always produce JSON output even if exit code is non-zero.
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
      echo "[pyright] Command: $pyright_cmd_str"
      set +e
      "${pyright_cmd[@]}" > "$out_json"
      pyright_rc=$?
      set -e

      # Validate JSON (pyright may emit non-JSON if it crashes).
      if ! "$json_py" -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$out_json" >/dev/null 2>&1; then
        echo "[pyright] Output was not valid JSON; overwriting with empty object." >&2
        echo '{}' > "$out_json"
      fi

      status="success"
      exit_code=0
      failure_category="unknown"
    fi
  fi
fi

error_excerpt=""
if [[ "$status" == "failure" ]]; then
  error_excerpt="$(tail -n 220 "$log_file" || true)"
fi

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_FILE="$log_file" \
STATUS="$status" EXIT_CODE="$exit_code" FAILURE_CATEGORY="$failure_category" SKIP_REASON="$skip_reason" \
PYRIGHT_CMD="$pyright_cmd_str" PYRIGHT_RC="$pyright_rc" PYRIGHT_LEVEL="$pyright_level" \
PYTHON_EXE="$python_exe" PYTHON_VER="$python_ver" GIT_COMMIT="$git_commit" \
PY_RESOLUTION="$python_resolution" PY_RESOLUTION_WARNING="$python_resolution_warning" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" DECISION_REASON="$decision_reason" \
ERROR_EXCERPT="$error_excerpt" \
  "$json_py" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_file = pathlib.Path(os.environ["LOG_FILE"])

def empty_assets():
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    pyright_data = {}

diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {
        m.group(1)
        for d in missing_diags
        for m in [pattern.search(d.get("message", ""))]
        if m
    }
)

def iter_py_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    exclude_dirs = {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "build",
        "dist",
        "node_modules",
        "build_output",
        "benchmark_assets",
        "benchmark_scripts",
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set:
    pkgs: set = set()
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

repo_root = pathlib.Path(".").resolve()
all_imported_packages = set()
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
    "pyright": pyright_data,
    "meta": {
        "files_scanned": files_scanned,
        "python_executable": os.environ.get("PYTHON_EXE", ""),
        "python_version": os.environ.get("PYTHON_VER", ""),
        "pyright_level": os.environ.get("PYRIGHT_LEVEL", ""),
        "pyright_command": os.environ.get("PYRIGHT_CMD", ""),
        "pyright_return_code": os.environ.get("PYRIGHT_RC", ""),
        "python_resolution": os.environ.get("PY_RESOLUTION", ""),
        "python_resolution_warning": os.environ.get("PY_RESOLUTION_WARNING", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": os.environ.get("STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("EXIT_CODE", "1") or "1"),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", "") or "pyright_not_run",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": empty_assets(),
    "meta": {
        "python": os.environ.get("PYTHON_EXE", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "python_resolution": os.environ.get("PY_RESOLUTION", ""),
        "python_resolution_warning": os.environ.get("PY_RESOLUTION_WARNING", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", "") or "",
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

PY

exit "$exit_code"

