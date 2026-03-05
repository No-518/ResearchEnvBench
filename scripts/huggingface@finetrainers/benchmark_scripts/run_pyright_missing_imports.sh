#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (fixed):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository/project to analyze

Python/environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report (required)

Optional:
  --report-path <path>           Agent report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --out-dir <path>               Root output dir (default: build_output)
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright

Notes:
  - If pyright is missing in the selected environment, this script MUST attempt:
      "<python>" -m pip install -q pyright
  - Non-zero pyright exit does NOT fail the stage; only inability to run/analyze does.
EOF
}

mode="system"
repo=""
report_path=""
out_root="build_output"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      mode="${2:-}"; shift 2 ;;
    --repo)
      repo="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --out-dir)
      out_root="${2:-}"; shift 2 ;;
    --level)
      pyright_level="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --venv)
      venv_dir="${2:-}"; shift 2 ;;
    --conda-env)
      conda_env="${2:-}"; shift 2 ;;
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
out_root="$(cd "$repo" && mkdir -p "$out_root" && cd "$out_root" && pwd)"
stage_dir="$out_root/pyright"
mkdir -p "$stage_dir"

log_path="$stage_dir/log.txt"
pyright_out="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

mkdir -p "$(dirname "$log_path")"
exec > >(tee "$log_path") 2>&1

cd "$repo"

echo "[run_pyright_missing_imports] repo=$repo"
echo "[run_pyright_missing_imports] out_root=$out_root"

sys_python="$(command -v python3 || command -v python || true)"

resolve_python_from_report() {
  if [[ -z "$sys_python" ]]; then
    return 1
  fi
  "$sys_python" benchmark_scripts/runner.py --print-python --report-path "$report_path" 2>/dev/null || return 1
}

py_cmd=()
python_resolution_source=""

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution_source="cli"
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      python_resolution_source="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution_source="uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_resolution_source="conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_resolution_source="poetry"
      ;;
    system)
      if ! resolved="$(resolve_python_from_report)"; then
        echo "Failed to resolve python from agent report; provide --python or use --mode venv/uv/conda/poetry." >&2
        cat >"$pyright_out" <<'JSON'
{"error":"missing_report"}
JSON
        cat >"$analysis_json" <<'JSON'
{"error":"missing_report"}
JSON
        "$sys_python" - <<PY || true
import json, os, subprocess
from pathlib import Path
repo = ${repo@Q}
stage_dir = ${stage_dir@Q}
log_path = Path(stage_dir) / "log.txt"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"(resolve python from report)",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python": "",
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES"] if os.environ.get(k)},
    "decision_reason":"missing/invalid agent report; cannot resolve python_path"
  },
  "failure_category":"missing_report",
  "error_excerpt": "\\n".join(log_path.read_text(errors="replace").splitlines()[-220:]) if log_path.exists() else "",
}
(Path(stage_dir) / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
        exit 1
      fi
      py_cmd=("$resolved")
      python_resolution_source="report"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "[run_pyright_missing_imports] python_cmd=${py_cmd[*]}"

python_exe=""
python_version=""
if ! python_exe="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null)"; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  cat >"$pyright_out" <<'JSON'
{"error":"python_invocation_failed"}
JSON
  cat >"$analysis_json" <<'JSON'
{"error":"python_invocation_failed"}
JSON
  "$sys_python" - <<PY || true
import json, os, subprocess, sys
from pathlib import Path
repo = ${repo@Q}
stage_dir = ${stage_dir@Q}
results_path = Path(stage_dir) / "results.json"
log_path = Path(stage_dir) / "log.txt"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command": ${py_cmd[*]@Q},
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python": "",
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES"] if os.environ.get(k)},
    "decision_reason":"python could not be invoked"
  },
  "failure_category":"deps",
  "error_excerpt": (log_path.read_text(errors="replace") if log_path.exists() else "")[-8000:],
}
results_path.write_text(json.dumps(payload, indent=2) + "\\n")
PY
  exit 1
fi

python_version="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

install_attempted=0
install_cmd=""
install_exit_code=0

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$repo/benchmark_assets/cache/pip}"
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[run_pyright_missing_imports] pyright missing; attempting install: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  install_exit_code=$?
  set -e
  if [[ "$install_exit_code" -ne 0 ]]; then
    echo "[run_pyright_missing_imports] pyright install failed (exit=$install_exit_code)"
    cat >"$pyright_out" <<'JSON'
{"error":"pyright_install_failed"}
JSON
    cat >"$analysis_json" <<'JSON'
{"error":"pyright_install_failed"}
JSON

    install_failure_category="deps"
    if grep -E "(Temporary failure in name resolution|Name or service not known|Connection( |-)error|ReadTimeout|ProxyError|HTTPSConnectionPool|No route to host)" "$log_path" >/dev/null 2>&1; then
      install_failure_category="download_failed"
    fi

    "$sys_python" - <<PY || true
import json, os, subprocess
from pathlib import Path
repo = ${repo@Q}
stage_dir = ${stage_dir@Q}
log_path = Path(stage_dir) / "log.txt"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command": ${install_cmd@Q},
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python": ${python_exe@Q},
    "python_version": ${python_version@Q},
    "python_resolution_source": ${python_resolution_source@Q},
    "pyright_install_attempted": True,
    "pyright_install_command": ${install_cmd@Q},
    "pyright_install_exit_code": ${install_exit_code},
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES","PIP_CACHE_DIR"] if os.environ.get(k)},
    "decision_reason":"pyright not available and install failed"
  },
  "failure_category": ${install_failure_category@Q},
  "error_excerpt": "\\n".join(log_path.read_text(errors="replace").splitlines()[-220:]) if log_path.exists() else "",
}
(Path(stage_dir) / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
    exit 1
  fi
fi

project_args=()
targets=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="Detected pyrightconfig.json; using --project pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && grep -E "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  decision_reason="Detected [tool.pyright] in pyproject.toml; using --project pyproject.toml"
elif [[ -d "src" ]]; then
  targets=(src)
  [[ -d "tests" ]] && targets+=(tests)
  decision_reason="Detected src/ layout; targeting src (and tests/ if present)"
else
  pkg_dirs_tmp=()
  while IFS= read -r init_file; do
    init_file="${init_file#./}"
    pkg_dirs_tmp+=("$(dirname "$init_file")")
  done < <(find . -maxdepth 2 -type f -name "__init__.py" \
    -not -path "./.git/*" \
    -not -path "./.venv/*" \
    -not -path "./venv/*" \
    -not -path "./build_output/*" \
    -not -path "./benchmark_assets/*" \
    -not -path "./benchmark_scripts/*" \
    -print)
  if [[ "${#pkg_dirs_tmp[@]}" -gt 0 ]]; then
    mapfile -t pkg_dirs < <(printf "%s\n" "${pkg_dirs_tmp[@]}" | sort -u)
  else
    pkg_dirs=()
  fi
  if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="Detected package directories via __init__.py; targeting those dirs"
  fi
fi

if [[ "${#project_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  echo "Could not auto-detect a Pyright project/targets (no pyrightconfig.json, no [tool.pyright], no src/, no packages)." >&2
  cat >"$pyright_out" <<'JSON'
{"error":"no_pyright_targets_detected"}
JSON
  cat >"$analysis_json" <<'JSON'
{"error":"no_pyright_targets_detected"}
JSON
  "$sys_python" - <<PY || true
import json, os, subprocess
from pathlib import Path
repo = ${repo@Q}
stage_dir = ${stage_dir@Q}
log_path = Path(stage_dir) / "log.txt"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"(auto-detect targets)",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python": ${python_exe@Q},
    "python_version": ${python_version@Q},
    "python_resolution_source": ${python_resolution_source@Q},
    "pyright_install_attempted": bool(${install_attempted}),
    "pyright_install_command": ${install_cmd@Q},
    "pyright_install_exit_code": ${install_exit_code},
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES"] if os.environ.get(k)},
    "decision_reason":"Failed to detect pyright targets"
  },
  "failure_category":"entrypoint_not_found",
  "error_excerpt": "\\n".join(log_path.read_text(errors="replace").splitlines()[-220:]) if log_path.exists() else "",
}
(Path(stage_dir) / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
  exit 1
fi

pyright_cmd=("${py_cmd[@]}" -m pyright "${project_args[@]}" "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}")
echo "[run_pyright_missing_imports] running: ${pyright_cmd[*]}"

set +e
"${pyright_cmd[@]}" >"$pyright_out"
pyright_exit_code=$?
set -e

export OUT_JSON="$pyright_out"
export ANALYSIS_JSON="$analysis_json"
export RESULTS_JSON="$results_json"
export PYTHON_EXE="$python_exe"
export PYTHON_VERSION="$python_version"
export PYRIGHT_EXIT_CODE="$pyright_exit_code"
export PYRIGHT_CMD_STR="${pyright_cmd[*]}"
export INSTALL_ATTEMPTED="$install_attempted"
export INSTALL_CMD="$install_cmd"
export INSTALL_EXIT_CODE="$install_exit_code"
export MODE="$mode"
export PY_CMD_STR="${py_cmd[*]}"
export DECISION_REASON="$decision_reason"
export PYTHON_RESOLUTION_SOURCE="$python_resolution_source"
export TARGETS_LIST="$(printf "%s\n" "${targets[@]:-}")"

"${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
from typing import Iterable

repo_root = pathlib.Path(".").resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

def git_commit() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL)
            .decode("utf-8", errors="replace")
            .strip()
        )
    except Exception:
        return ""

def iter_py_files(roots: Iterable[pathlib.Path]) -> Iterable[pathlib.Path]:
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
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
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

pyright_payload = {}
pyright_parse_error = ""
try:
    pyright_payload = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    pyright_parse_error = str(e)

diagnostics = []
if isinstance(pyright_payload, dict):
    diagnostics = pyright_payload.get("generalDiagnostics", []) or []

missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]
pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
)

targets = [t for t in (os.environ.get("TARGETS_LIST", "").splitlines()) if t.strip()]

roots = [repo_root] if not targets else [repo_root / t for t in targets]
all_imported_packages = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis = {
    "missing_packages": missing_packages,
    "pyright": pyright_payload,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD_STR", ""),
        "python_executable": os.environ.get("PYTHON_EXE", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "python_resolution_source": os.environ.get("PYTHON_RESOLUTION_SOURCE", ""),
        "targets": targets,
        "pyright_command": os.environ.get("PYRIGHT_CMD_STR", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or 0),
        "pyright_output_parse_error": pyright_parse_error,
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or 0)),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "pyright_install_exit_code": int(os.environ.get("INSTALL_EXIT_CODE", "0") or 0),
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

status = "success"
failure_category = ""
error_excerpt = ""
if pyright_parse_error:
    status = "failure"
    failure_category = "invalid_json"
    error_excerpt = pyright_parse_error

results = {
    "status": status,
    "skip_reason": "not_applicable",
    "exit_code": 0 if status == "success" else 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_EXE", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "git_commit": git_commit(),
        "env_vars": {k: os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON", "CUDA_VISIBLE_DEVICES"] if os.environ.get(k)},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or 0)),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "pyright_install_exit_code": int(os.environ.get("INSTALL_EXIT_CODE", "0") or 0),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or 0),
    },
    "failure_category": failure_category,
    "error_excerpt": error_excerpt,
    "metrics": analysis["metrics"],
}

results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

if [[ -f "$results_json" ]]; then
  stage_exit_code="$("$sys_python" - <<PY
import json
from pathlib import Path
p=Path(${results_json@Q})
try:
  d=json.loads(p.read_text())
  print(int(d.get("exit_code", 1)))
except Exception:
  print(1)
PY
)"
  exit "$stage_exit_code"
fi

exit 1
