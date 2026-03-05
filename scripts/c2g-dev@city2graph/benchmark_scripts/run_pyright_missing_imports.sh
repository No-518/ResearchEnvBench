#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule: reportMissingImports).

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Python selection (highest priority first):
  --python <path>                Explicit python executable to use
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH
  (default)                      Use python_path from /opt/scimlopsbench/report.json

Optional:
  --repo <path>                  Repo root (default: auto = parent of benchmark_scripts)
  --out-dir <path>               Base output dir (default: build_output)
  --level <error|warning|...>    Pyright diagnostic level (default: error)
  -- <pyright args...>           Extra args passed to pyright

Notes:
  - Ensures `pyright` is importable; installs via pip if missing:
      <python> -m pip install -q pyright
  - Does not fail the stage just because pyright returns non-zero.
EOF
}

mode=""
repo=""
out_dir="build_output"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --out-base) out_dir="${2:-}"; shift 2 ;; # alias
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${repo:-$REPO_ROOT_DEFAULT}"

STAGE_DIR="$REPO_ROOT/$out_dir/pyright"
LOG_PATH="$STAGE_DIR/log.txt"
OUT_JSON="$STAGE_DIR/pyright_output.json"
ANALYSIS_JSON="$STAGE_DIR/analysis.json"
RESULTS_JSON="$STAGE_DIR/results.json"

mkdir -p "$STAGE_DIR"
: >"$LOG_PATH"

# Log all script output.
exec > >(tee -a "$LOG_PATH") 2>&1

cd "$REPO_ROOT"

stage_status="failure"
stage_exit_code=1
failure_category="unknown"
skip_reason="unknown"
install_attempted="false"
install_command=""
python_resolution="unknown"
pyright_cmd_display=""

has_tool_pyright() {
  local file="$1"
  if command -v rg >/dev/null 2>&1; then
    rg -n "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" "$file" >/dev/null 2>&1
  else
    grep -Eqs '^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$' "$file" 2>/dev/null
  fi
}

log_has_pattern() {
  local pattern="$1"
  local file="$2"
  if command -v rg >/dev/null 2>&1; then
    rg -n "$pattern" "$file" >/dev/null 2>&1
  else
    grep -Eqs "$pattern" "$file" 2>/dev/null
  fi
}

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli --python"
elif [[ -n "${mode}" ]]; then
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        failure_category="args_unknown"
      else
        py_cmd=("$venv_dir/bin/python")
        python_resolution="mode venv"
      fi
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution="mode uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        failure_category="args_unknown"
      else
        if ! command -v conda >/dev/null 2>&1; then
          echo "conda not found in PATH" >&2
          failure_category="deps"
        else
          py_cmd=(conda run -n "$conda_env" python)
          python_resolution="mode conda"
        fi
      fi
      ;;
    poetry)
      if ! command -v poetry >/dev/null 2>&1; then
        echo "poetry not found in PATH" >&2
        failure_category="deps"
      else
        py_cmd=(poetry run python)
        python_resolution="mode poetry"
      fi
      ;;
    system)
      py_cmd=(python)
      python_resolution="mode system"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      failure_category="args_unknown"
      ;;
  esac
else
  python_resolution="report python_path"
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  if [[ ! -f "$report_path" ]]; then
    echo "Missing report.json at: $report_path" >&2
    failure_category="missing_report"
    py_cmd=()
  else
    sys_py="$(command -v python3 || command -v python || true)"
    if [[ -z "$sys_py" ]]; then
      echo "No python found on PATH to parse report.json" >&2
      failure_category="deps"
      py_cmd=()
    else
      resolved="$("$sys_py" - <<PY || true
import json, os, sys
path = os.environ.get("SCIMLOPSBENCH_REPORT", "/opt/scimlopsbench/report.json")
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    p = data.get("python_path")
    if isinstance(p, str) and p.strip():
        print(p.strip())
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
)"
      if [[ -z "$resolved" ]]; then
        echo "Failed to resolve python_path from report.json ($report_path)" >&2
        failure_category="missing_report"
        py_cmd=()
      else
        py_cmd=("$resolved")
      fi
    fi
  fi
fi

# Ensure required output files exist even on early failures.
if [[ ! -f "$OUT_JSON" ]]; then
  printf '{}' >"$OUT_JSON"
fi

if [[ ${#py_cmd[@]} -eq 0 ]]; then
  echo "No usable python resolved; cannot run pyright." >&2
else
  echo "Using python: ${py_cmd[*]}"
  if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    echo "Failed to run python via: ${py_cmd[*]}" >&2
    failure_category="${failure_category:-deps}"
    py_cmd=()
  fi
fi

project_args=()
targets=()

if [[ ${#py_cmd[@]} -ne 0 ]]; then
  if [[ -f "pyrightconfig.json" ]]; then
    project_args=(--project pyrightconfig.json)
  elif [[ -f "pyproject.toml" ]] && has_tool_pyright "pyproject.toml"; then
    project_args=(--project pyproject.toml)
  elif [[ -d "src" ]]; then
    targets+=(src)
    [[ -d "tests" ]] && targets+=(tests)
  else
    # Detect package dirs (contain __init__.py). Prefer top-level modules.
    mapfile -t pkg_dirs < <(
      find . \
        -type d \( -name ".git" -o -name ".venv" -o -name "venv" -o -name "__pycache__" -o -name "build_output" -o -name "benchmark_assets" \) -prune -o \
        -type f -name "__init__.py" -print \
        2>/dev/null \
        | sed 's#/__init__\\.py$##' \
        | sed 's#^\\./##' \
        | sort -u
    )
    for d in "${pkg_dirs[@]:-}"; do
      [[ -n "$d" ]] && targets+=("$d")
    done
    [[ -d "tests" ]] && targets+=(tests)
  fi

  if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
    echo "No Python project targets found for Pyright." >&2
    failure_category="entrypoint_not_found"
  fi
fi

if [[ ${#py_cmd[@]} -ne 0 && "$failure_category" != "entrypoint_not_found" ]]; then
  # Ensure pyright available inside selected python.
  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
    install_attempted="true"
    install_command="${py_cmd[*]} -m pip install -q pyright"
    echo "pyright not found; attempting install: $install_command"
    if ! "${py_cmd[@]}" -m pip --version >/dev/null 2>&1; then
      echo "pip is not available in the selected python environment." >&2
      failure_category="deps"
    else
      if ! "${py_cmd[@]}" -m pip install -q pyright; then
        echo "pip install pyright failed." >&2
        # Best-effort categorize common offline errors.
        if log_has_pattern "(Temporary failure in name resolution|ConnectionError|ReadTimeout|No matching distribution found|Could not fetch URL)" "$LOG_PATH"; then
          failure_category="download_failed"
        else
          failure_category="deps"
        fi
      fi
    fi
  fi
fi

pyright_exit=0
if [[ ${#py_cmd[@]} -ne 0 && "$failure_category" == "unknown" ]]; then
  if [[ ${#project_args[@]} -ne 0 ]]; then
    pyright_cmd_display="${py_cmd[*]} -m pyright ${project_args[*]} --level $pyright_level --outputjson ${pyright_extra_args[*]}"
  else
    pyright_cmd_display="${py_cmd[*]} -m pyright ${targets[*]} --level $pyright_level --outputjson ${pyright_extra_args[*]}"
  fi
  echo "Running pyright..."
  echo "Pyright command: $pyright_cmd_display"
  mkdir -p "$(dirname "$OUT_JSON")"
  if [[ ${#project_args[@]} -ne 0 ]]; then
    # Project mode: rely on project file to define scope.
    set +e
    "${py_cmd[@]}" -m pyright "${project_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$OUT_JSON"
    pyright_exit=$?
    set -e
  else
    set +e
    "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$OUT_JSON"
    pyright_exit=$?
    set -e
  fi
  echo "pyright exit code: $pyright_exit (ignored for stage status)"
fi

# Always write analysis.json and results.json (even if earlier steps failed).
sys_py_fallback="$(command -v python3 || command -v python || true)"
writer_py=("${py_cmd[@]}")
if [[ ${#writer_py[@]} -eq 0 && -n "$sys_py_fallback" ]]; then
  writer_py=("$sys_py_fallback")
fi

if [[ ${#writer_py[@]} -eq 0 ]]; then
  echo "No python available to write analysis/results JSON." >&2
  # Last resort: minimal JSON.
  cat >"$ANALYSIS_JSON" <<'JSON'
{"missing_packages":[],"pyright":{},"meta":{},"metrics":{"missing_packages_count":0,"total_imported_packages_count":0,"missing_package_ratio":"0/0"}}
JSON
  cat >"$RESULTS_JSON" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "pyright (unavailable)",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": ""},
  "failure_category": "${failure_category:-deps}",
  "error_excerpt": "No python available to write results."
}
JSON
  exit 1
fi

TARGETS_JSON="$(
  printf '%s\n' "${targets[@]:-}" | "${writer_py[@]}" - <<'PY'
import json, sys
targets = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
print(json.dumps(targets))
PY
)"

GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
PYTHON_EXE="$("${writer_py[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
PYTHON_VER="$("${writer_py[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

export OUT_JSON ANALYSIS_JSON RESULTS_JSON LOG_PATH
export PYRIGHT_EXIT="$pyright_exit"
export FAILURE_CATEGORY="$failure_category"
export INSTALL_ATTEMPTED="$install_attempted"
export INSTALL_COMMAND="$install_command"
export PYTHON_RESOLUTION="$python_resolution"
export REPO_ROOT
export GIT_COMMIT PYTHON_EXE PYTHON_VER
export TARGETS_JSON
export PYRIGHT_CMD_DISPLAY="$pyright_cmd_display"

"${writer_py[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
from collections import deque
from typing import Iterable

repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_PATH"])

pyright_exit = int(os.environ.get("PYRIGHT_EXIT", "0") or "0")
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown") or "unknown"
install_attempted = os.environ.get("INSTALL_ATTEMPTED", "false") == "true"
install_command = os.environ.get("INSTALL_COMMAND", "")
python_resolution = os.environ.get("PYTHON_RESOLUTION", "")
git_commit = os.environ.get("GIT_COMMIT", "")
python_exe = os.environ.get("PYTHON_EXE", "")
python_ver = os.environ.get("PYTHON_VER", "")
targets = json.loads(os.environ.get("TARGETS_JSON", "[]"))
pyright_cmd_display = os.environ.get("PYRIGHT_CMD_DISPLAY", "")

def env_snapshot() -> dict:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "PYTHONPATH",
        "PATH",
        "HF_AUTH_TOKEN",
        "HF_TOKEN",
    ]
    out = {}
    for k in keys:
        if k not in os.environ:
            continue
        v = os.environ.get(k, "")
        if any(s in k.upper() for s in ("TOKEN", "SECRET", "KEY", "PASS")) and v:
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out

def tail_file(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])[-8000:]
    except Exception:
        return ""

error_excerpt = tail_file(log_path, 240)

base_metrics = {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
}

analysis_payload = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "python_resolution": python_resolution,
        "python_executable": python_exe,
        "python_version": python_ver,
        "pyright_exit_code": pyright_exit,
        "pyright_install_attempted": install_attempted,
        "pyright_install_command": install_command,
        "targets": targets,
        "pyright_command": pyright_cmd_display,
    },
    "metrics": base_metrics,
}

def write_results(status: str, exit_code: int, failure_category_out: str) -> None:
    payload = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "pyright",
        "task": "check",
        "command": analysis_payload["meta"].get("pyright_command", ""),
        "timeout_sec": 600,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": python_exe,
            "git_commit": git_commit,
            "env_vars": env_snapshot(),
            "decision_reason": analysis_payload["meta"].get("decision_reason", ""),
            "python_resolution": python_resolution,
            "pyright_install_attempted": install_attempted,
            "pyright_install_command": install_command,
        },
        "failure_category": failure_category_out,
        "error_excerpt": error_excerpt,
        "metrics": analysis_payload.get("metrics", base_metrics),
    }
    results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

if failure_category in ("missing_report", "deps", "download_failed", "args_unknown", "entrypoint_not_found"):
    analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_results("failure", 1, failure_category)
    raise SystemExit(1)

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    analysis_payload["meta"]["parse_error"] = str(e)
    analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_results("failure", 1, "invalid_json")
    raise SystemExit(1)

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
)

def iter_py_files(roots: list[pathlib.Path]) -> Iterable[pathlib.Path]:
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
    for root in roots:
        if not root.exists():
            continue
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

roots = [repo_root / t for t in targets] if targets else [repo_root / "city2graph", repo_root / "tests"]
all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload["missing_packages"] = missing_packages
analysis_payload["pyright"] = data
analysis_payload["metrics"] = {
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}
analysis_payload["meta"]["files_scanned"] = files_scanned
analysis_payload["meta"]["decision_reason"] = (
    "Targets chosen by priority: pyrightconfig.json > pyproject.toml[tool.pyright] > src/ > package dirs."
)
analysis_payload["meta"]["pyright_command"] = analysis_payload["meta"].get("pyright_command") or "pyright --outputjson (see log)"

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")

# Stage success: we successfully ran and parsed pyright JSON (even if pyright exit != 0).
write_results("success", 0, "unknown")
print(missing_package_ratio)
PY

exit_code=$?
exit "$exit_code"
