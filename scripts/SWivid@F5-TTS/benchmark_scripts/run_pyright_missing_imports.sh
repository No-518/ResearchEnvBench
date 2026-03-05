#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (reportMissingImports).

Required:
  --repo <path>                  Path to the repository/project to analyze

Environment selection (optional):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --report-path <path>           Agent report.json path override (default: SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <int>            Default: 600
  -- <pyright args...>           Extra args passed to Pyright

Outputs:
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json
EOF
}

repo=""
out_dir="build_output/pyright"
pyright_level="error"
timeout_sec="600"

mode=""
python_bin=""
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
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

repo_root="$(cd "$repo" && pwd)"
cd "$repo_root"

mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
pyright_out="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec > >(tee "$log_path") 2>&1

BOOTSTRAP_PY="$(command -v python >/dev/null 2>&1 && echo python || echo python3)"
timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

py_cmd=()
python_source="unknown"
python_resolution_err=""

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_source="cli"
elif [[ -n "$mode" ]]; then
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      python_source="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_source="uv"
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_source="conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_source="poetry"
      ;;
    system)
      py_cmd=(python)
      python_source="system"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
else
  rp_args=()
  [[ -n "$report_path" ]] && rp_args+=(--report-path "$report_path")
  resolved="$("$BOOTSTRAP_PY" benchmark_scripts/runner.py resolve-python --require-report "${rp_args[@]}" || true)"
  py="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("python",""))' <<<"$resolved" 2>/dev/null || true)"
  err="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error",""))' <<<"$resolved" 2>/dev/null || true)"
  if [[ -z "$py" || -n "$err" ]]; then
    python_resolution_err="${err:-missing_report}"
    py_cmd=()
  else
    py_cmd=("$py")
    python_source="report"
  fi
fi

write_failure_json() {
  local failure_category="$1"
  local error_excerpt="$2"
  local command_str="$3"
  local install_attempted="$4"
  local install_command="$5"
  local install_ok="$6"

  mkdir -p "$out_dir"
  [[ -f "$pyright_out" ]] || printf '%s\n' '{}' > "$pyright_out"
  [[ -f "$analysis_json" ]] || printf '%s\n' '{}' > "$analysis_json"

  "$BOOTSTRAP_PY" - <<PY
import json
from pathlib import Path

out = Path(${results_json@Q})
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": ${command_str@Q},
  "timeout_sec": int(${timeout_sec}),
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": ${python_bin@Q} if ${python_bin@Q} else "",
    "git_commit": ${git_commit@Q},
    "env_vars": {},
    "decision_reason": "pyright missing-import scan",
    "timestamp_utc": ${timestamp_utc@Q},
    "python_source": ${python_source@Q},
    "pyright_install": {
      "attempted": bool(int(${install_attempted})),
      "command": ${install_command@Q},
      "success": bool(int(${install_ok})),
    },
  },
  "failure_category": ${failure_category@Q},
  "error_excerpt": ${error_excerpt@Q},
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
}

if [[ ${#py_cmd[@]} -eq 0 ]]; then
  write_failure_json "missing_report" "python resolution failed: ${python_resolution_err}" "" "0" "" "0"
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  write_failure_json "path_hallucination" "Failed to run python via: ${py_cmd[*]}" "${py_cmd[*]}" "0" "" "0"
  exit 1
fi

install_attempted=0
install_ok=0
install_command=""

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_command="${py_cmd[*]} -m pip install -q pyright"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  install_rc=$?
  set -e
  if [[ $install_rc -ne 0 ]]; then
    fc="deps"
    if grep -Eqi "Connection|Temporary failure|Name or service not known|No route to host|Network is unreachable|TLS|CERTIFICATE" "$log_path" 2>/dev/null; then
      fc="download_failed"
    fi
    write_failure_json "$fc" "pyright install failed (rc=$install_rc). See log.txt for details." "$install_command" "1" "$install_command" "0"
    exit 1
  fi
  install_ok=1
fi

project_args=()
targets=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="pyrightconfig.json detected; using --project pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && grep -qE '^\[tool\.pyright\]' pyproject.toml; then
  project_args=(--project pyproject.toml)
  decision_reason="pyproject.toml contains [tool.pyright]; using --project pyproject.toml"
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
  decision_reason="src/ layout detected; analyzing src (and tests if present)"
else
  mapfile -t targets < <("$BOOTSTRAP_PY" - <<'PY'
from pathlib import Path

root = Path(".").resolve()
exclude = {
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

init_dirs = set()
for p in root.rglob("__init__.py"):
    if any(part in exclude for part in p.parts):
        continue
    init_dirs.add(str(p.parent.relative_to(root)))

print("\n".join(sorted(init_dirs)))
PY
)
  if [[ ${#targets[@]} -gt 0 ]]; then
    decision_reason="No pyright config; using detected package dirs with __init__.py"
  else
    write_failure_json "entrypoint_not_found" "No pyrightconfig.json, no [tool.pyright] in pyproject.toml, no src/ layout, and no package dirs detected." "" "$install_attempted" "$install_command" "$install_ok"
    exit 1
  fi
fi

if [[ ${#project_args[@]} -gt 0 ]]; then
  cmd_str="${py_cmd[*]} -m pyright ${project_args[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
else
  cmd_str="${py_cmd[*]} -m pyright ${targets[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
fi

set +e
if [[ ${#project_args[@]} -gt 0 ]]; then
  "${py_cmd[@]}" -m pyright "${project_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" > "$pyright_out"
else
  "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" > "$pyright_out"
fi
pyright_rc=$?
set -e

[[ -s "$pyright_out" ]] || printf '%s\n' '{}' > "$pyright_out"

PYTHON_EXE="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)')"
PYTHON_VERSION="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())')"

export OUT_JSON="$pyright_out"
export ANALYSIS_JSON="$analysis_json"
export RESULTS_JSON="$results_json"
export PY_CMD_STR="$cmd_str"
export PYRIGHT_RC="$pyright_rc"
export TIMEOUT_SEC="$timeout_sec"
export PYTHON_EXE
export PYTHON_VERSION
export GIT_COMMIT="$git_commit"
export DECISION_REASON="$decision_reason"
export INSTALL_ATTEMPTED="$install_attempted"
export INSTALL_COMMAND="$install_command"
export INSTALL_OK="$install_ok"
export TIMESTAMP_UTC="$timestamp_utc"

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
repo_root = pathlib.Path(".").resolve()

def safe_load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

data = safe_load_json(out_json)
diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

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

def iter_py_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set:
    pkgs: set = set()
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

all_imported_packages = set()
files_scanned = 0
for py_file in iter_py_files(repo_root):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "python": os.environ.get("PYTHON_EXE", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_RC", "0") or "0"),
        "files_scanned": files_scanned,
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "success",
    "skip_reason": "unknown",
    "exit_code": 0,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PY_CMD_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600") or "600"),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_EXE", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install": {
            "attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")),
            "command": os.environ.get("INSTALL_COMMAND", ""),
            "success": bool(int(os.environ.get("INSTALL_OK", "0") or "0")),
        },
        "timestamp_utc": os.environ.get("TIMESTAMP_UTC", ""),
    },
    "failure_category": "unknown",
    "error_excerpt": "",
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "missing_packages": missing_packages,
    "pyright_exit_code": int(os.environ.get("PYRIGHT_RC", "0") or "0"),
}

if files_scanned == 0:
    results_payload["status"] = "failure"
    results_payload["exit_code"] = 1
    results_payload["failure_category"] = "entrypoint_not_found"
    results_payload["error_excerpt"] = "No Python files discovered in repository."

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

exit_code="$("$BOOTSTRAP_PY" -c 'import json,sys; print(int(json.load(open(sys.argv[1],"r",encoding="utf-8")).get("exit_code",1)))' "$results_json" 2>/dev/null || echo 1)"
exit "$exit_code"
