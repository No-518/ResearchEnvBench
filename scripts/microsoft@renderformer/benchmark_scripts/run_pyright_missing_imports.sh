#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule: reportMissingImports).

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
  --mode system                  Use python from report.json (preferred) or PATH

Optional:
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright
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
    --mode)
      mode="${2:-}"; shift 2 ;;
    --repo)
      repo="${2:-}"; shift 2 ;;
    --out-dir)
      out_dir="${2:-}"; shift 2 ;;
    --level)
      pyright_level="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
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

mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

cd "$repo"

if [[ -z "$report_path" ]]; then
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
fi

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
      else
        if [[ ! -f "$report_path" ]]; then
          echo "[pyright] missing report: $report_path" >&2
          echo '{}' >"$out_json"
          echo '{}' >"$analysis_json"
          python3 - <<PY
import json, pathlib, subprocess
out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
def tail(path, n=220):
  try:
    lines=path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
  except Exception:
    return ""
git_commit=""
try:
  git_commit=subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
except Exception:
  git_commit=""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"python -m pyright <auto>",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":git_commit,"env_vars":{},"decision_reason":"missing report.json; cannot resolve python_path"},
  "failure_category":"missing_report",
  "error_excerpt":tail(log_path),
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
          exit 1
        fi

        resolved_report_python="$(
          python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
p=Path(${report_path@Q})
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("python_path","") or "")
except Exception:
  print("")
PY
        )"
        if [[ -z "$resolved_report_python" ]]; then
          echo "[pyright] report missing python_path: $report_path" >&2
          echo '{}' >"$out_json"
          echo '{}' >"$analysis_json"
          python3 - <<PY
import json, pathlib, subprocess
out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
def tail(path, n=220):
  try:
    lines=path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
  except Exception:
    return ""
git_commit=""
try:
  git_commit=subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
except Exception:
  git_commit=""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"python -m pyright <auto>",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":git_commit,"env_vars":{},"decision_reason":"report.json missing python_path"},
  "failure_category":"missing_report",
  "error_excerpt":tail(log_path),
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
          exit 1
        fi
        py_cmd=("$resolved_report_python")
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

python_cmd_str="${py_cmd[*]}"
echo "[pyright] python: $python_cmd_str"

set +e
"${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1
py_ok=$?
set -e
if [[ "$py_ok" -ne 0 ]]; then
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  python3 - <<PY
import json, pathlib, time, subprocess
out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
def tail(path, n=220):
  try:
    lines=path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
  except Exception:
    return ""
git_commit=""
try:
  git_commit=subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
except Exception:
  git_commit=""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":${python_cmd_str@Q},
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":${python_cmd_str@Q},"git_commit":git_commit,"env_vars":{},"decision_reason":"failed to execute selected python"},
  "failure_category":"missing_report",
  "error_excerpt":tail(log_path),
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
  exit 1
fi

export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$(pwd)/benchmark_assets/cache/pip}"
mkdir -p "$PIP_CACHE_DIR" || true

install_attempted=0
install_cmd=""
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="$python_cmd_str -m pip install -q pyright"
  echo "[pyright] installing: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pip_rc=$?
  set -e
  if [[ "$pip_rc" -ne 0 ]]; then
    echo '{}' >"$out_json"
    echo '{}' >"$analysis_json"
    python3 - <<PY
import json, pathlib, subprocess
out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
def tail(path, n=220):
  try:
    lines=path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
  except Exception:
    return ""
git_commit=""
try:
  git_commit=subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
except Exception:
  git_commit=""
excerpt=tail(log_path)
failure_category="deps"
for needle in ["Temporary failure in name resolution","ConnectionError","Read timed out","Could not fetch","No matching distribution found","Could not find a version"]:
  if needle in excerpt:
    failure_category="download_failed"
    break
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":${install_cmd@Q},
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python":${python_cmd_str@Q},
    "git_commit":git_commit,
    "env_vars":{"PIP_CACHE_DIR":${PIP_CACHE_DIR@Q}},
    "decision_reason":"pyright missing; attempted install",
    "pyright_install_attempted":True,
    "pyright_install_command":${install_cmd@Q},
  },
  "failure_category":failure_category,
  "error_excerpt":excerpt,
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
    exit 1
  fi
fi

decision_reason=""
project_args=()
targets=()

if [[ -f pyrightconfig.json ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="pyrightconfig.json found; using --project pyrightconfig.json"
elif [[ -f pyproject.toml ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  decision_reason="pyproject.toml has [tool.pyright]; using --project pyproject.toml"
elif [[ -d src ]]; then
  targets=(src)
  [[ -d tests ]] && targets+=(tests)
  decision_reason="src/ layout detected; targets=src (and tests if present)"
else
  mapfile -t pkg_dirs < <(
    find . -maxdepth 4 -type f -name "__init__.py" \
      ! -path "./.git/*" ! -path "./.venv/*" ! -path "./venv/*" ! -path "./build/*" ! -path "./dist/*" \
      ! -path "./benchmark_assets/*" ! -path "./build_output/*" ! -path "./benchmark_scripts/*" \
      -print0 | xargs -0 -n1 dirname | sed 's#^\\./##' | sort -u
  )
  if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="detected python packages via __init__.py; targets=${targets[*]}"
  fi
fi

if [[ -z "$decision_reason" ]]; then
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  python3 - <<PY
import json, pathlib, subprocess
out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
def tail(path, n=220):
  try:
    lines=path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
  except Exception:
    return ""
git_commit=""
try:
  git_commit=subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip()
except Exception:
  git_commit=""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"python -m pyright <auto-targets>",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python":${python_cmd_str@Q},
    "git_commit":git_commit,
    "env_vars":{},
    "decision_reason":"no pyrightconfig/pyproject/src/packages detected",
    "pyright_install_attempted":bool(${install_attempted}),
    "pyright_install_command":${install_cmd@Q},
  },
  "failure_category":"entrypoint_not_found",
  "error_excerpt":tail(log_path),
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
  exit 1
fi

cmd=("${py_cmd[@]}" -m pyright)
if [[ "${#project_args[@]}" -gt 0 ]]; then
  cmd+=("${project_args[@]}")
fi
if [[ "${#targets[@]}" -gt 0 ]]; then
  cmd+=("${targets[@]}")
fi
cmd+=(--level "$pyright_level" --outputjson)
if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
  cmd+=("${pyright_extra_args[@]}")
fi

echo "[pyright] running: ${cmd[*]}"
set +e
"${cmd[@]}" >"$out_json"
pyright_rc=$?
set -e
if [[ ! -s "$out_json" ]]; then
  echo '{"generalDiagnostics":[]}' >"$out_json"
fi

TARGETS_JSON="$(
  if [[ "${#targets[@]}" -eq 0 ]]; then
    echo "[]"
  else
    printf '%s\n' "${targets[@]}" | python3 - <<'PY'
import json, sys
items=[ln.strip() for ln in sys.stdin if ln.strip()]
print(json.dumps(items))
PY
  fi
)"

PROJECT_ARGS_JSON="$(
  if [[ "${#project_args[@]}" -eq 0 ]]; then
    echo "[]"
  else
    printf '%s\n' "${project_args[@]}" | python3 - <<'PY'
import json, sys
items=[ln.strip() for ln in sys.stdin if ln.strip()]
print(json.dumps(items))
PY
  fi
)"

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_PATH="$log_path" \
MODE="$mode" PY_CMD="$python_cmd_str" PYRIGHT_EXIT_CODE="$pyright_rc" \
PYRIGHT_LEVEL="$pyright_level" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" \
DECISION_REASON="$decision_reason" TARGETS_JSON="$TARGETS_JSON" PROJECT_ARGS_JSON="$PROJECT_ARGS_JSON" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
from typing import Iterable, List

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_PATH"])

def tail(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])

git_commit = ""
try:
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
except Exception:
    git_commit = ""

pyright_data = {}
try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    pyright_data = {"generalDiagnostics": []}

diagnostics = pyright_data.get("generalDiagnostics", []) or []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
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
    "benchmark_assets",
    "benchmark_scripts",
    "build_output",
}

def iter_py_files(paths: List[pathlib.Path]) -> Iterable[pathlib.Path]:
    for root in paths:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.exists():
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

repo_root = pathlib.Path(".").resolve()
targets = []
try:
    targets = [pathlib.Path(p) for p in json.loads(os.environ.get("TARGETS_JSON", "[]"))]
except Exception:
    targets = []
scan_roots = targets or [repo_root]

all_imported_packages = set()
files_scanned = 0
for py_file in iter_py_files([repo_root / p for p in scan_roots]):
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
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or 0),
        "pyright_install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "targets": json.loads(os.environ.get("TARGETS_JSON", "[]") or "[]"),
        "project_args": json.loads(os.environ.get("PROJECT_ARGS_JSON", "[]") or "[]"),
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")

status = "success"
failure_category = "unknown"
exit_code = 0

results_payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": "",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": git_commit,
        "env_vars": {"PIP_CACHE_DIR": os.environ.get("PIP_CACHE_DIR", "")} if os.environ.get("PIP_CACHE_DIR") else {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or 0),
    },
    "failure_category": failure_category,
    "error_excerpt": tail(log_path),
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

cmd_str = os.environ.get("PY_CMD", "") + " -m pyright"
if os.environ.get("PROJECT_ARGS_JSON"):
    try:
        for a in json.loads(os.environ["PROJECT_ARGS_JSON"]):
            cmd_str += " " + a
    except Exception:
        pass
if os.environ.get("TARGETS_JSON"):
    try:
        for t in json.loads(os.environ["TARGETS_JSON"]):
            cmd_str += " " + t
    except Exception:
        pass
cmd_str += f" --level {os.environ.get('PYRIGHT_LEVEL','error')} --outputjson"
results_payload["command"] = cmd_str.strip()

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(missing_package_ratio)
PY

exit 0
