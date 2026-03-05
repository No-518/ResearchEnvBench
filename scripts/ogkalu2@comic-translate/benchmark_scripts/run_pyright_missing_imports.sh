#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (always, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Repository/project root

Python environment selection (pick ONE):
  --python <path>                Explicit python executable (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --out-dir <path>               Output base dir (default: build_output)
  --level <error|warning|...>    Pyright level (default: error)
  --timeout-sec <int>            Timeout for Pyright (default: 600)
  --report-path <path>           Agent report path (default: /opt/scimlopsbench/report.json)
  -- <pyright args...>           Extra args passed to Pyright

Notes:
  - Pyright is installed into the selected interpreter if missing.
  - Non-zero Pyright exit code does not fail this stage.
EOF
}

mode="auto"
repo=""
out_base="build_output"
pyright_level="error"
timeout_sec="600"
python_bin=""
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_base="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
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

# Prevent creating __pycache__ in the repo or environment.
export PYTHONDONTWRITEBYTECODE=1

repo="$(python -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$repo" 2>/dev/null || echo "$repo")"
stage_dir="$repo/$out_base/pyright"
mkdir -p "$stage_dir"

log_path="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

echo "stage=pyright"
echo "repo=$repo"
echo "out_dir=$stage_dir"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

status="failure"
skip_reason="unknown"
failure_category="unknown"
install_attempted="false"
install_command=""
decision_reason=""
command_str=""

cd "$repo" || {
  echo "Failed to cd to repo: $repo" >&2
  failure_category="entrypoint_not_found"
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  echo '{}' >"$results_json"
  exit 1
}

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "${mode}" in
    auto)
      # Follow runner.py priority: SCIMLOPSBENCH_PYTHON > report.json python_path > PATH python.
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
      else
        rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
        if [[ -f "$rp" ]]; then
          py_from_report="$(python - "$rp" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    v = data.get("python_path", "")
    print(v if isinstance(v, str) else "")
except Exception:
    print("")
PY
)"
          if [[ -n "$py_from_report" ]]; then
            py_cmd=("$py_from_report")
          else
            py_cmd=(python)
          fi
        else
          echo "Missing report.json and no --python/SCIMLOPSBENCH_PYTHON provided: $rp" >&2
          failure_category="missing_report"
          echo '{}' >"$out_json"
          echo '{}' >"$analysis_json"
          echo '{}' >"$results_json"
          exit 1
        fi
      fi
      ;;
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "python_cmd=${py_cmd[*]}"
if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  failure_category="missing_report"
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  echo '{}' >"$results_json"
  exit 1
fi

# Ensure pyright is installed in the selected interpreter.
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted="true"
  install_command="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null) -m pip install -q pyright"
  echo "pyright not found; attempting install: $install_command"
  if ! "${py_cmd[@]}" -m pip install -q pyright; then
    echo "Failed to install pyright." >&2
    # Offline installs can look like download errors; keep deps vs download_failed distinction best-effort.
    if grep -E "Temporary failure in name resolution|No matching distribution|Connection (timed out|refused)" "$log_path" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    echo '{}' >"$out_json"
    # Still produce analysis/results with empty payloads + failure metadata.
    OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
      STAGE_STATUS="failure" FAILURE_CATEGORY="$failure_category" SKIP_REASON="$skip_reason" \
      PY_CMD="${py_cmd[*]}" MODE="$mode" TIMEOUT_SEC="$timeout_sec" COMMAND_STR="" \
      INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" DECISION_REASON="" \
      "${py_cmd[@]}" - <<'PY' || true
import json, os, pathlib

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = analysis_json.parent / "log.txt"

def tail(path: pathlib.Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""

analysis = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    },
}
analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

stage_results = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": "",
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "deps"),
    "error_excerpt": tail(log_path),
    **analysis["metrics"],
}
results_json.write_text(json.dumps(stage_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
    exit 1
  fi
fi

# Determine pyright targets/project.
pyright_args=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  pyright_args+=(--project pyrightconfig.json)
  decision_reason="Found pyrightconfig.json; using --project pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && grep -q '^\[tool\.pyright\]' pyproject.toml >/dev/null 2>&1; then
  pyright_args+=(--project pyproject.toml)
  decision_reason="Found [tool.pyright] in pyproject.toml; using --project pyproject.toml"
elif [[ -d "src" ]]; then
  targets+=(src)
  [[ -d "tests" ]] && targets+=(tests)
  decision_reason="Detected src/ layout; running on ${targets[*]}"
else
  mapfile -t pkg_dirs < <(find . -maxdepth 2 -type f -name "__init__.py" 2>/dev/null | awk -F/ '{print $2}' | sort -u)
  filtered=()
  for d in "${pkg_dirs[@]:-}"; do
    case "$d" in
      ""|.git|.venv|venv|build|dist|node_modules|__pycache__|benchmark_assets|benchmark_scripts|build_output) ;;
      *) filtered+=("$d") ;;
    esac
  done
  if [[ ${#filtered[@]} -gt 0 ]]; then
    targets=("${filtered[@]}")
    decision_reason="Detected package dirs with __init__.py; running on ${targets[*]}"
  else
    echo "Could not determine Pyright targets (no config, no src/, no packages)." >&2
    failure_category="entrypoint_not_found"
    echo '{}' >"$out_json"
    OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
      STAGE_STATUS="failure" FAILURE_CATEGORY="$failure_category" SKIP_REASON="$skip_reason" \
      PY_CMD="${py_cmd[*]}" MODE="$mode" TIMEOUT_SEC="$timeout_sec" COMMAND_STR="" \
      INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" DECISION_REASON="$decision_reason" \
      "${py_cmd[@]}" - <<'PY' || true
import json, os, pathlib

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = analysis_json.parent / "log.txt"

def tail(path: pathlib.Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""

analysis = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    },
}
analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

stage_results = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": "",
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "entrypoint_not_found"),
    "error_excerpt": tail(log_path),
    **analysis["metrics"],
}
results_json.write_text(json.dumps(stage_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
    exit 1
  fi
fi

echo "Pyright decision_reason: $decision_reason"
command_str="${py_cmd[*]} -m pyright ${targets[*]} --level $pyright_level --outputjson ${pyright_args[*]} ${pyright_extra_args[*]}"
echo "command=$command_str"

# Run pyright; do not fail stage on non-zero exit from pyright itself.
pyright_rc=0
timeout "${timeout_sec}s" "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_args[@]}" "${pyright_extra_args[@]}" >"$out_json" || pyright_rc=$?
if [[ "$pyright_rc" -ne 0 ]]; then
  echo "Pyright exited non-zero (rc=$pyright_rc); continuing (stage still succeeds if parsing succeeds)."
fi

if [[ ! -s "$out_json" ]]; then
  echo '{}' >"$out_json"
fi

# Analyze output and write analysis.json + results.json (with required stage fields).
if ! OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
  STAGE_STATUS="success" FAILURE_CATEGORY="unknown" SKIP_REASON="$skip_reason" \
  PY_CMD="${py_cmd[*]}" MODE="$mode" TIMEOUT_SEC="$timeout_sec" COMMAND_STR="$command_str" \
  INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" DECISION_REASON="$decision_reason" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = analysis_json.parent / "log.txt"
repo_root = pathlib.Path(".").resolve()

def tail(path: pathlib.Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import \"([^\"]+)\"')
missing_packages = sorted(
    {
        (m.group(1).split(".")[0] if m.group(1) else "")
        for d in missing_diags
        if isinstance(d, dict) and (m := pattern.search(d.get("message", "") or ""))
    }
)
missing_packages = [p for p in missing_packages if p]

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
        "benchmark_scripts",
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
                if alias.name:
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

analysis = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

stage_results = {
    "status": os.environ.get("STAGE_STATUS", "success"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": 0,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": "",
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": "" if os.environ.get("STAGE_STATUS", "success") == "success" else tail(log_path),
    **analysis["metrics"],
}
results_json.write_text(json.dumps(stage_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY
then
  echo "Failed to post-process Pyright output." >&2
  status="failure"
  failure_category="invalid_json"
  # Ensure required JSON artifacts still exist.
  OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
    STAGE_STATUS="failure" FAILURE_CATEGORY="$failure_category" SKIP_REASON="$skip_reason" \
    PY_CMD="${py_cmd[*]}" MODE="$mode" TIMEOUT_SEC="$timeout_sec" COMMAND_STR="$command_str" \
    INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" DECISION_REASON="$decision_reason" \
    "${py_cmd[@]}" - <<'PY' || true
import json, os, pathlib

analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = analysis_json.parent / "log.txt"

def tail(path: pathlib.Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""

analysis = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    },
}
analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

stage_results = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": "",
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", ""),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "invalid_json"),
    "error_excerpt": tail(log_path),
    **analysis["metrics"],
}
results_json.write_text(json.dumps(stage_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

status="success"
failure_category="unknown"

if [[ "$status" == "success" ]]; then
  exit 0
fi
exit 1
