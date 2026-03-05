#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Default behavior (no explicit env selection):
  - Uses python_path from the agent report (SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json).
  - If the report is missing/invalid, the stage fails with failure_category="missing_report".

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                 Repo root (default: current working directory)

Environment selection (optional, overrides report-based python):
  --python <path>               Explicit python executable to use (highest priority)
  --mode venv   --venv <path>   Use <venv>/bin/python
  --mode uv    [--venv <path>]  Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n> Use: conda run -n <n> python
  --mode poetry                 Use: poetry run python
  --mode system                 Use: python from PATH

Optional:
  --report-path <path>          Agent report path (default: SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --level <error|warning|...>   Default: error
  -- <pyright args...>          Extra args passed to Pyright
EOF
}

repo="."
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
mode=""
mode_set=0
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; mode_set=1; shift 2 ;;
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

repo="${repo:-.}"
repo="$(cd "$repo" && pwd)"
mkdir -p "$out_dir"

log_file="$out_dir/log.txt"
pyright_out="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_file"
: >"$pyright_out"
: >"$analysis_json"
: >"$results_json"

exec > >(tee -a "$log_file") 2>&1

stage="pyright"
task="check"
timeout_sec=600
framework="unknown"

repo_root="$repo"
cd "$repo_root"

export BENCHMARK_ASSETS_DIR="$repo_root/benchmark_assets"
export BENCHMARK_CACHE_DIR="$BENCHMARK_ASSETS_DIR/cache"
export HOME="$BENCHMARK_CACHE_DIR/home"
export XDG_CACHE_HOME="$BENCHMARK_CACHE_DIR/xdg"
export TMPDIR="$BENCHMARK_CACHE_DIR/tmp"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$TMPDIR"

report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

py_cmd=()
python_resolution="report"
resolved_python_path=""

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli_python"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("${SCIMLOPSBENCH_PYTHON}")
  python_resolution="env_SCIMLOPSBENCH_PYTHON"
elif [[ "$mode_set" -eq 1 ]]; then
  python_resolution="mode:${mode}"
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        python_resolution="error"
      else
        py_cmd=("$venv_dir/bin/python")
      fi
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        python_resolution="error"
      else
        command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; python_resolution="error"; }
        py_cmd=(conda run -n "$conda_env" python)
      fi
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; python_resolution="error"; }
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      python_resolution="error"
      ;;
  esac
else
  python_resolution="report_python_path"
  if [[ ! -f "$report_path" ]]; then
    echo "Agent report missing at $report_path" >&2
    python_resolution="missing_report"
  else
    resolved_python_path="$(python3 - <<PY 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(${report_path@Q})
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("python_path","") or "")
except Exception:
    print("")
PY
)"
    if [[ -z "$resolved_python_path" ]]; then
      echo "Agent report invalid or missing python_path at $report_path" >&2
      python_resolution="missing_report"
    else
      py_cmd=("$resolved_python_path")
    fi
  fi
fi

install_attempted=0
install_command=""
install_exit_code=0
pyright_invocation=""
decision_reason=""
failure_category=""
status="success"
exit_code=0

write_results() {
  local status_in="$1"
  local exit_code_in="$2"
  local failure_cat_in="$3"
  local decision_reason_in="$4"
  local python_cmd_str="$5"
  local pyright_invocation_in="$6"
  python3 - <<PY
import json, os, pathlib, subprocess, time

repo_root = pathlib.Path(${repo_root@Q})
out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
results_path = out_dir / "results.json"

def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def tail_lines(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if len(lines) > n else "\n".join(lines)
    except Exception:
        return ""

assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

payload = {
    "status": ${status_in@Q},
    "skip_reason": "not_applicable",
    "exit_code": int(${exit_code_in@Q}),
    "stage": "pyright",
    "task": "check",
    "command": ${pyright_invocation_in@Q},
    "timeout_sec": int(${timeout_sec@Q}),
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": (os.popen("python3 -c 'import platform; print(platform.python_version())'").read().strip()),
        "git_commit": git_commit(),
        "env_vars": {k: os.environ.get(k, "") for k in [
            "SCIMLOPSBENCH_REPORT",
            "SCIMLOPSBENCH_PYTHON",
            "HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
        ] if k in os.environ},
        "decision_reason": ${decision_reason_in@Q},
        "python_cmd": ${python_cmd_str@Q},
        "python_resolution": ${python_resolution@Q},
        "report_path": ${report_path@Q},
        "pyright_level": ${pyright_level@Q},
        "install_attempted": bool(int(${install_attempted@Q})),
        "install_command": ${install_command@Q},
        "install_exit_code": int(${install_exit_code@Q}),
    },
    "failure_category": ${failure_cat_in@Q},
    "error_excerpt": tail_lines(log_path),
}

tmp = results_path.with_suffix(".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(results_path)
PY
}

if [[ "$python_resolution" == "error" ]]; then
  status="failure"
  exit_code=1
  failure_category="args_unknown"
  decision_reason="python selection failed (invalid --mode / missing required flags)"
  write_results "$status" "$exit_code" "$failure_category" "$decision_reason" "" ""
  exit 1
fi

if [[ "$python_resolution" == "missing_report" ]]; then
  status="failure"
  exit_code=1
  failure_category="missing_report"
  decision_reason="No explicit python provided; agent report missing/invalid, cannot resolve python_path."
  write_results "$status" "$exit_code" "$failure_category" "$decision_reason" "" ""
  exit 1
fi

if [[ "${#py_cmd[@]}" -eq 0 ]]; then
  status="failure"
  exit_code=1
  failure_category="missing_report"
  decision_reason="Failed to resolve python interpreter."
  write_results "$status" "$exit_code" "$failure_category" "$decision_reason" "" ""
  exit 1
fi

echo "[pyright] repo_root=$repo_root"
echo "[pyright] report_path=$report_path"
echo "[pyright] python_cmd=${py_cmd[*]}"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  status="failure"
  exit_code=1
  failure_category="deps"
  decision_reason="Python command could not be executed."
  write_results "$status" "$exit_code" "$failure_category" "$decision_reason" "${py_cmd[*]}" ""
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_command="${py_cmd[*]} -m pip install -q pyright"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  install_exit_code=$?
  set -e
  if [[ "$install_exit_code" -ne 0 ]]; then
    status="failure"
    exit_code=1
    failure_category="deps"
    decision_reason="Pyright not available and installation failed."
    write_results "$status" "$exit_code" "$failure_category" "$decision_reason" "${py_cmd[*]}" ""
    exit 1
  fi
fi

project_arg=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  project_arg=(--project "pyrightconfig.json")
  decision_reason="Using pyrightconfig.json (--project pyrightconfig.json)."
elif [[ -f "pyproject.toml" ]] && python3 - <<'PY' >/dev/null 2>&1
import re, pathlib
txt = pathlib.Path("pyproject.toml").read_text(encoding="utf-8", errors="ignore")
print("yes" if re.search(r"^\\[tool\\.pyright\\]\\s*$", txt, flags=re.M) else "no")
PY
then
  project_arg=(--project "pyproject.toml")
  decision_reason="Using [tool.pyright] in pyproject.toml (--project pyproject.toml)."
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
  decision_reason="Using src/ layout targets."
else
  mapfile -t targets < <(find . -maxdepth 2 -type f -name '__init__.py' \
    | sed -e 's|^\\./||' -e 's|/__init__\\.py$||' \
    | rg -v '^(\\.git|\\.venv|venv|build|dist|node_modules|build_output|benchmark_assets|benchmark_scripts)(/|$)' \
    | sort -u)
  if [[ "${#targets[@]}" -gt 0 ]]; then
    decision_reason="Using detected package dirs with __init__.py."
  fi
fi

if [[ "${#project_arg[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  status="failure"
  exit_code=1
  failure_category="entrypoint_not_found"
  decision_reason="No pyrightconfig/pyproject tool.pyright/src layout/package dirs detected."
  write_results "$status" "$exit_code" "$failure_category" "$decision_reason" "${py_cmd[*]}" ""
  exit 1
fi

echo "[pyright] decision_reason=$decision_reason"
echo "[pyright] project_arg=${project_arg[*]:-<none>}"
echo "[pyright] targets=${targets[*]:-<none>}"

set +e
if [[ "${#project_arg[@]}" -gt 0 ]]; then
  pyright_invocation="${py_cmd[*]} -m pyright ${project_arg[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]:-}"
  "${py_cmd[@]}" -m pyright "${project_arg[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$pyright_out"
else
  pyright_invocation="${py_cmd[*]} -m pyright ${targets[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]:-}"
  "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$pyright_out"
fi
pyright_exit=$?
set -e

echo "[pyright] pyright_exit=$pyright_exit (non-zero is allowed)"

# Parse output; always produce analysis.json and results.json.
PYRIGHT_OUT_JSON="$pyright_out" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_FILE="$log_file" \
MODE="$mode" PY_CMD="${py_cmd[*]}" \
DECISION_REASON="$decision_reason" \
PYRIGHT_INVOCATION="$pyright_invocation" PYRIGHT_EXIT="$pyright_exit" PYRIGHT_LEVEL="$pyright_level" \
PYTHON_RESOLUTION="$python_resolution" REPORT_PATH="$report_path" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" INSTALL_EXIT_CODE="$install_exit_code" \
python3 - <<'PY'
import ast
import json
import os
import pathlib
import platform
import re
import subprocess
from typing import Iterable

repo_root = pathlib.Path(".").resolve()
out_json = pathlib.Path(os.environ["PYRIGHT_OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_FILE"])

def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def safe_load_json(path: pathlib.Path):
    raw = ""
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}, False, "empty_output"
        return json.loads(raw), True, ""
    except Exception as e:
        return {}, False, repr(e)

data, parse_ok, parse_error = safe_load_json(out_json)
diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []

missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]
pattern = re.compile(r'Import \"([^.\\\"]+)')
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

def collect_imported_packages(py_file: pathlib.Path) -> set:
    pkgs: set = set()
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
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_invocation": os.environ.get("PYRIGHT_INVOCATION", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT", "0") or 0),
        "pyright_level": os.environ.get("PYRIGHT_LEVEL", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
        "report_path": os.environ.get("REPORT_PATH", ""),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "install_exit_code": int(os.environ.get("INSTALL_EXIT_CODE", "0") or 0),
        "pyright_output_parse_ok": bool(parse_ok),
        "pyright_output_parse_error": parse_error,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def tail_lines(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if len(lines) > n else "\n".join(lines)
    except Exception:
        return ""

status = "success" if parse_ok else "failure"
exit_code = 0 if status == "success" else 1
failure_category = "" if status == "success" else "invalid_json"
error_excerpt = "" if status == "success" else tail_lines(log_path)

results_payload = {
    "status": status,
    "skip_reason": "not_applicable",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_INVOCATION", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": platform.python_version(),
        "git_commit": git_commit(),
        "env_vars": {k: os.environ.get(k, "") for k in [
            "SCIMLOPSBENCH_REPORT",
            "SCIMLOPSBENCH_PYTHON",
            "HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
        ] if k in os.environ},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
        "report_path": os.environ.get("REPORT_PATH", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT", "0") or 0),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "install_exit_code": int(os.environ.get("INSTALL_EXIT_CODE", "0") or 0),
    },
    "failure_category": failure_category,
    "error_excerpt": error_excerpt,
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

exit "$(python3 - <<PY 2>/dev/null || echo 1
import json, pathlib
p = pathlib.Path(${results_json@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(int(d.get("exit_code", 1)))
except Exception:
    print(1)
PY
)"
