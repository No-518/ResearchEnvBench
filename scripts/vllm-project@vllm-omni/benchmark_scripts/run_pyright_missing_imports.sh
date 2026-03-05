#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (reportMissingImports).

Outputs (always):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository/project to analyze

Environment selection (pick ONE; default: report.json python_path when available):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --report-path <path>           Override report.json path (default: /opt/scimlopsbench/report.json)
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <n>              Default: 600
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonversion 3.12)

Examples:
  benchmark_scripts/run_pyright_missing_imports.sh --repo .
  benchmark_scripts/run_pyright_missing_imports.sh --mode venv --venv .venv --repo .
  benchmark_scripts/run_pyright_missing_imports.sh --python /abs/path/to/python --repo .
EOF
}

repo=""
out_dir="build_output/pyright"
pyright_level="error"
timeout_sec="600"

python_bin=""
mode=""
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

mode_set=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      repo="${2:-}"; shift 2 ;;
    --out-dir)
      out_dir="${2:-}"; shift 2 ;;
    --level)
      pyright_level="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --mode)
      mode="${2:-}"; mode_set=1; shift 2 ;;
    --venv)
      venv_dir="${2:-}"; shift 2 ;;
    --conda-env)
      conda_env="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
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
cd "$repo"

mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec > >(tee -a "$log_path") 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"

python_cmd=()
python_source=""
python_warning=""
python_resolution_failed=0
python_resolution_error=""

resolve_report_python() {
  local rp="$1"
  local py_exec=""
  if command -v python3 >/dev/null 2>&1; then
    py_exec="python3"
  elif command -v python >/dev/null 2>&1; then
    py_exec="python"
  else
    return 1
  fi
  "$py_exec" - "$rp" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    sys.exit(2)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(3)
val = data.get("python_path")
if not isinstance(val, str) or not val:
    sys.exit(4)
print(val)
PY
}

if [[ -n "$python_bin" ]]; then
  python_cmd=("$python_bin")
  python_source="cli"
else
  if [[ "$mode_set" -eq 0 ]]; then
    # Default: use python_path from report.json if available; otherwise system python.
    if [[ -z "$report_path" ]]; then
      report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
    fi
    if py_from_report="$(resolve_report_python "$report_path" 2>/dev/null)"; then
      python_cmd=("$py_from_report")
      python_source="report"
    else
      python_cmd=(python)
      python_source="system_fallback"
      python_warning="report python_path unavailable; using PATH python"
    fi
  else
    case "$mode" in
      venv)
        if [[ -z "$venv_dir" ]]; then
          echo "--venv is required for --mode venv" >&2
          python_resolution_failed=1
          python_resolution_error="missing --venv for --mode venv"
        else
          python_cmd=("$venv_dir/bin/python")
          python_source="venv"
        fi
        ;;
      uv)
        venv_dir="${venv_dir:-.venv}"
        python_cmd=("$venv_dir/bin/python")
        python_source="uv"
        ;;
      conda)
        if [[ -z "$conda_env" ]]; then
          echo "--conda-env is required for --mode conda" >&2
          python_resolution_failed=1
          python_resolution_error="missing --conda-env for --mode conda"
        else
          command -v conda >/dev/null 2>&1 || { python_resolution_failed=1; python_resolution_error="conda not found in PATH"; }
          python_cmd=(conda run -n "$conda_env" python)
          python_source="conda"
        fi
        ;;
      poetry)
        command -v poetry >/dev/null 2>&1 || { python_resolution_failed=1; python_resolution_error="poetry not found in PATH"; }
        python_cmd=(poetry run python)
        python_source="poetry"
        ;;
      system)
        python_cmd=(python)
        python_source="system"
        ;;
      *)
        echo "Unknown --mode: $mode" >&2
        python_resolution_failed=1
        python_resolution_error="unknown --mode: $mode"
        ;;
    esac
  fi
fi

echo "[pyright] python_cmd=${python_cmd[*]}"
echo "[pyright] python_source=$python_source"
if [[ -n "$python_warning" ]]; then
  echo "[pyright] python_warning=$python_warning"
fi

touch "$out_json"
echo "{}" > "$out_json"

stage_status="failure"
failure_category="unknown"
skip_reason="unknown"
stage_exit_code=1

decision_reason=""

project_args=()
targets=()
targets_csv=""

if [[ "$python_resolution_failed" -eq 1 ]]; then
  echo "[pyright] python resolution failed: $python_resolution_error"
  failure_category="missing_report"
else
  if ! "${python_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    echo "[pyright] failed to run python via: ${python_cmd[*]}"
    failure_category="missing_report"
  else
    if [[ -f "pyrightconfig.json" ]]; then
      project_args=(--project pyrightconfig.json)
      decision_reason="Found pyrightconfig.json; using --project pyrightconfig.json"
    elif [[ -f "pyproject.toml" ]] && { command -v rg >/dev/null 2>&1 && rg -n "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml >/dev/null 2>&1 || grep -Eq "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml; }; then
      project_args=(--project pyproject.toml)
      decision_reason="Found [tool.pyright] in pyproject.toml; using --project pyproject.toml"
    elif [[ -d "src" ]]; then
      targets=(src)
      [[ -d "tests" ]] && targets+=(tests)
      decision_reason="Detected src/ layout; targeting src (and tests if present)"
    else
      while IFS= read -r d; do
        [[ -n "$d" ]] && targets+=("$d")
      done < <(find . -maxdepth 1 -mindepth 1 -type d -exec sh -c 'test -f "$1/__init__.py" && echo "${1#./}"' _ {} \; | sort)

      if [[ "${#targets[@]}" -gt 0 ]]; then
        targets_csv="$(IFS=,; echo "${targets[*]}")"
        decision_reason="Detected top-level package dirs via __init__.py; targeting: ${targets[*]}"
      else
        echo "[pyright] no targets detected (no pyright config, no src/, no package dirs)"
        failure_category="entrypoint_not_found"
      fi
    fi
  fi
fi

install_attempted=0
install_command=""
install_rc=0

if [[ "$failure_category" != "entrypoint_not_found" && "$failure_category" != "missing_report" ]]; then
  if ! "${python_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
    install_attempted=1
    install_command="${python_cmd[*]} -m pip install -q pyright"
    echo "[pyright] pyright not available; attempting install: $install_command"
    set +e
    "${python_cmd[@]}" -m pip install -q pyright
    install_rc=$?
    set -e
    if [[ "$install_rc" -ne 0 ]]; then
      echo "[pyright] pyright install failed (rc=$install_rc)"
      if { command -v rg >/dev/null 2>&1 && rg -n "Temporary failure in name resolution|Connection (timed out|reset)|No matching distribution found|Could not fetch URL|SSLError" "$log_path" >/dev/null 2>&1 || grep -Eq "Temporary failure in name resolution|Connection (timed out|reset)|No matching distribution found|Could not fetch URL|SSLError" "$log_path"; }; then
        failure_category="download_failed"
      else
        failure_category="deps"
      fi
    fi
  fi
fi

pyright_rc=0
pyright_ran=0
pyright_cmd_str=""

if [[ "$failure_category" == "unknown" ]]; then
  pyright_ran=1
  cmd=( "${python_cmd[@]}" -m pyright )
  if [[ "${#project_args[@]}" -gt 0 ]]; then
    cmd+=( "${project_args[@]}" )
  else
    cmd+=( "${targets[@]}" )
  fi
  cmd+=( --level "$pyright_level" --outputjson )
  if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
    cmd+=( "${pyright_extra_args[@]}" )
  fi

  pyright_cmd_str="${cmd[*]}"
  echo "[pyright] running: $pyright_cmd_str"

  set +e
  if command -v timeout >/dev/null 2>&1; then
    timeout --preserve-status "${timeout_sec}" "${cmd[@]}" >"$out_json"
    pyright_rc=$?
  else
    "${cmd[@]}" >"$out_json"
    pyright_rc=$?
  fi
  set -e

  echo "[pyright] pyright_exit_code=$pyright_rc (ignored for stage status unless output is invalid)"
fi

python_analysis_exec=""
if command -v python3 >/dev/null 2>&1; then
  python_analysis_exec="python3"
elif command -v python >/dev/null 2>&1; then
  python_analysis_exec="python"
fi

if [[ -z "$python_analysis_exec" ]]; then
  echo "[pyright] cannot find python to write analysis/results"
  failure_category="deps"
else
  OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
    STAGE_STATUS="$stage_status" FAILURE_CATEGORY="$failure_category" STAGE_EXIT_CODE="$stage_exit_code" \
    PYRIGHT_CMD="$pyright_cmd_str" PYRIGHT_RC="$pyright_rc" PYRIGHT_RAN="$pyright_ran" \
    PYTHON_CMD="${python_cmd[*]}" PYTHON_SOURCE="$python_source" PYTHON_WARNING="$python_warning" \
    INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" INSTALL_RC="$install_rc" \
    TIMEOUT_SEC="$timeout_sec" DECISION_REASON="$decision_reason" LOG_PATH="$log_path" TARGETS_CSV="$targets_csv" \
    "$python_analysis_exec" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

repo_root = pathlib.Path(".").resolve()

def safe_read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

pyright_data = safe_read_json(out_json)
diagnostics = pyright_data.get("generalDiagnostics", [])
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import\\s+\\"([^\\"]+)\\"')
missing_packages = sorted(
    {pattern.search(d.get("message", "")).group(1).split(".")[0] for d in missing_diags if pattern.search(d.get("message", ""))}
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
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.is_dir():
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

cmd_str = os.environ.get("PYRIGHT_CMD", "")
roots: list[pathlib.Path] = []
targets_csv = os.environ.get("TARGETS_CSV", "").strip()
if targets_csv:
    for tok in [t for t in targets_csv.split(",") if t]:
        p = (repo_root / tok).resolve()
        if p.exists():
            roots.append(p)
if not roots:
    roots = [repo_root]

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": pyright_data,
    "meta": {
        "python_cmd": os.environ.get("PYTHON_CMD", ""),
        "python_source": os.environ.get("PYTHON_SOURCE", ""),
        "python_warning": os.environ.get("PYTHON_WARNING", ""),
        "pyright_ran": bool(int(os.environ.get("PYRIGHT_RAN", "0"))),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_RC", "0")),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "install_exit_code": int(os.environ.get("INSTALL_RC", "0")),
        "files_scanned": files_scanned,
        "scan_roots": [str(p) for p in roots],
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""

def tail_log(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)

log_path = pathlib.Path(os.environ.get("LOG_PATH", "build_output/pyright/log.txt"))

status = "success"
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown") or ""
error_excerpt = ""
exit_code = 0

pyright_valid_json = bool(pyright_data)
if failure_category in {"entrypoint_not_found", "missing_report", "deps", "download_failed"}:
    status = "failure"
    exit_code = 1
    error_excerpt = tail_log(log_path)
elif not pyright_valid_json and bool(int(os.environ.get("PYRIGHT_RAN", "0"))):
    status = "failure"
    exit_code = 1
    failure_category = "invalid_json"
    error_excerpt = tail_log(log_path)
elif bool(int(os.environ.get("PYRIGHT_RAN", "0"))) and int(os.environ.get("PYRIGHT_RC", "0")) == 124:
    status = "failure"
    exit_code = 1
    failure_category = "timeout"
    error_excerpt = tail_log(log_path)

result_payload = {
    "status": status,
    "skip_reason": "not_applicable" if status == "success" else "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": cmd_str,
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_CMD", ""),
        "git_commit": git_commit(),
        "env_vars": {k: os.environ[k] for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON", "PYTHONPATH"] if k in os.environ},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright": {
            "python_cmd": os.environ.get("PYTHON_CMD", ""),
            "python_source": os.environ.get("PYTHON_SOURCE", ""),
            "python_warning": os.environ.get("PYTHON_WARNING", ""),
            "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
            "install_command": os.environ.get("INSTALL_COMMAND", ""),
            "install_exit_code": int(os.environ.get("INSTALL_RC", "0")),
            "pyright_exit_code": int(os.environ.get("PYRIGHT_RC", "0")),
        },
        "timestamp_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    },
    "failure_category": failure_category,
    "error_excerpt": error_excerpt,
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

results_json.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
sys.exit(exit_code)
PY
  rc=$?

  if [[ "$rc" -eq 0 ]]; then
    stage_status="success"
    stage_exit_code=0
  else
    stage_status="failure"
    stage_exit_code=1
  fi
  exit "$rc"
fi

echo "[pyright] unexpected: failed to write analysis/results"
exit 1
