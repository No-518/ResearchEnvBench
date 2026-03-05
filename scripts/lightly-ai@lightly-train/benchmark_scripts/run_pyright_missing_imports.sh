#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs:
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Repository/project path to analyze

Python selection (pick ONE):
  --python <path>                Explicit python executable (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report (/opt/scimlopsbench/report.json)

Optional:
  --report-path <path>           Override report path for --mode system
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright
EOF
}

mode="system"
repo=""
report_path=""
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
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

repo="$(cd "$repo" && pwd)"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

out_dir="$repo_root/build_output/pyright"
mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec >"$log_file" 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"
echo "[pyright] mode=$mode"
echo "[pyright] level=$pyright_level"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
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
      # Use python from agent report via runner.py resolution.
      resolver=(python3 "$script_dir/runner.py" resolve-python)
      if [[ -n "$report_path" ]]; then
        resolver+=(--report-path "$report_path")
      fi
      py_bin="$("${resolver[@]}")" || true
      if [[ -z "${py_bin:-}" ]]; then
        echo "[pyright] ERROR: failed to resolve python from report" >&2
        cat >"$results_json" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "python3 benchmark_scripts/runner.py resolve-python",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": null,
    "git_commit": null,
    "env_vars": {},
    "decision_reason": "Unable to resolve python from report for pyright stage."
  },
  "failure_category": "missing_report",
  "error_excerpt": "failed to resolve python from report"
}
EOF
        printf '%s\n' '{}' >"$out_json"
        printf '%s\n' '{}' >"$analysis_json"
        exit 1
      fi
      py_cmd=("$py_bin")
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "[pyright] python_cmd=${py_cmd[*]}"

cd "$repo"

target_args=()
decision_reason=""
if [[ -f "pyrightconfig.json" ]]; then
  target_args=(--project pyrightconfig.json)
  decision_reason="Using pyrightconfig.json as project."
elif [[ -f "pyproject.toml" ]]; then
  if command -v rg >/dev/null 2>&1; then
    if rg -n "^\\[tool\\.pyright\\]" -S pyproject.toml >/dev/null 2>&1; then
      target_args=(--project pyproject.toml)
      decision_reason="Using [tool.pyright] in pyproject.toml as project."
    fi
  else
    if grep -Eq '^\\[tool\\.pyright\\]' pyproject.toml >/dev/null 2>&1; then
      target_args=(--project pyproject.toml)
      decision_reason="Using [tool.pyright] in pyproject.toml as project."
    fi
  fi
elif [[ -d "src" ]]; then
  target_args=(src)
  [[ -d "tests" ]] && target_args+=(tests)
  decision_reason="No pyright config; using src/ (and tests/) layout targets."
else
  mapfile -t pkg_dirs < <(
    find . -maxdepth 2 -type f -name "__init__.py" \
      | sed 's|^\\./||' \
      | awk -F/ '{print $1}' \
      | sort -u \
      | grep -Ev "^(\\.git|build_output|benchmark_assets|benchmark_scripts|\\.venv|venv|dist|build|node_modules)$" || true
  )
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    target_args=("${pkg_dirs[@]}")
    decision_reason="No pyright config; using detected package dirs with __init__.py."
  else
    echo "[pyright] ERROR: no pyrightconfig/pyproject tool.pyright/src/ or packages detected"
    printf '%s\n' '{}' >"$out_json"
    printf '%s\n' '{}' >"$analysis_json"
    cat >"$results_json" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "python -m pyright <targets>",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": null,
    "git_commit": null,
    "env_vars": {},
    "decision_reason": "No suitable python targets detected for pyright."
  },
  "failure_category": "entrypoint_not_found",
  "error_excerpt": "No suitable python targets detected for pyright."
}
EOF
    exit 1
  fi
fi

install_attempted=0
install_cmd=""
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] Installing pyright: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pip_rc=$?
  set -e
  if [[ $pip_rc -ne 0 ]]; then
    echo "[pyright] ERROR: pyright installation failed (rc=$pip_rc)"
    printf '%s\n' '{}' >"$out_json"
    printf '%s\n' '{}' >"$analysis_json"
    cat >"$results_json" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "$install_cmd",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "${py_cmd[*]}",
    "git_commit": null,
    "env_vars": {},
    "decision_reason": "Attempted to install pyright into selected environment but pip failed.",
    "pyright_install": {"attempted": true, "command": "$install_cmd", "exit_code": $pip_rc}
  },
  "failure_category": "deps",
  "error_excerpt": "pyright installation failed"
}
EOF
    exit 1
  fi
fi

pyright_cmd=("${py_cmd[@]}" -m pyright "${target_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}")
echo "[pyright] Running: ${pyright_cmd[*]}"
set +e
"${pyright_cmd[@]}" >"$out_json"
pyright_rc=$?
set -e
echo "[pyright] pyright_exit_code=$pyright_rc (ignored for stage success)"

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
REPO_ROOT="$repo" PY_CMD="${py_cmd[*]}" PYRIGHT_CMD="${pyright_cmd[*]}" PYRIGHT_EXIT_CODE="$pyright_rc" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" DECISION_REASON="$decision_reason" \
python3 - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
from collections import deque
from datetime import datetime, timezone
from typing import Iterable

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def git_commit(repo: pathlib.Path) -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None

def tail_lines(path: pathlib.Path, n: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq = deque(f, maxlen=n)
        return "".join(dq).strip()
    except Exception:
        return ""

repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = results_json.parent / "log.txt"

pyright_raw = {}
pyright_parse_error = None
try:
    pyright_raw = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as exc:
    pyright_parse_error = str(exc)

diagnostics = pyright_raw.get("generalDiagnostics", []) if isinstance(pyright_raw, dict) else []
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
        "benchmark_scripts",
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set[str]:
    pkgs: set[str] = set()
    try:
        src = py_file.read_text(encoding="utf-8", errors="ignore")
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

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": pyright_raw,
    "meta": {
        "python_cmd": os.environ.get("PY_CMD", ""),
        "pyright_cmd": os.environ.get("PYRIGHT_CMD", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or "0"),
        "pyright_json_parse_error": pyright_parse_error,
        "files_scanned": files_scanned,
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install": {
            "attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
            "command": os.environ.get("INSTALL_CMD", ""),
        },
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

status = "success"
failure_category = ""
exit_code = 0
if pyright_raw == {} and pyright_parse_error is not None:
    status = "failure"
    failure_category = "runtime"
    exit_code = 1

results_payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "metrics": analysis_payload["metrics"],
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": git_commit(repo_root),
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "started_utc": utc_now(),
        "pyright_install": analysis_payload["meta"]["pyright_install"],
    },
    "failure_category": failure_category,
    "error_excerpt": tail_lines(log_path, 220) if status == "failure" else "",
}
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

raise SystemExit(exit_code)
PY
