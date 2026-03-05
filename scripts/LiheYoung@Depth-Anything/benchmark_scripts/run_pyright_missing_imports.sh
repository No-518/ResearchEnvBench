#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH
  --mode auto                    Resolve python from $SCIMLOPSBENCH_REPORT via runner.py (default)

Optional:
  --repo <path>                  Default: repository root
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonversion 3.10)

Notes:
  - If pyright is missing in the selected environment, this script attempts:
      "<resolved python>" -m pip install -q pyright
  - Pyright non-zero exit does not fail this stage by itself (still produces JSON outputs).
EOF
}

repo=""
mode="auto"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      repo="${2:-}"; shift 2 ;;
    --mode)
      mode="${2:-}"; shift 2 ;;
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

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="${repo:-$repo_root}"

stage_dir="$repo/build_output/pyright"
mkdir -p "$stage_dir"

log_file="$stage_dir/log.txt"
pyright_out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

sys_py="$(command -v python3 || command -v python || true)"

status="failure"
skip_reason="unknown"
exit_code=1
failure_category="unknown"
decision_reason=""
command_str=""
install_attempted=0
install_cmd=""
install_rc=""

write_results() {
  if [[ -z "$sys_py" ]]; then
    printf '%s\n' "FATAL: python/python3 not found to write structured JSON; writing minimal results." >>"$log_file"
    printf '%s\n' "{}" >"$pyright_out_json" 2>/dev/null || true
    printf '%s\n' "{}" >"$analysis_json" 2>/dev/null || true
    cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "python/python3 not found"},
  "failure_category": "deps",
  "error_excerpt": "python/python3 not found on PATH"
}
JSON
    return
  fi
  STAGE_DIR="$stage_dir" \
  STATUS="$status" \
  SKIP_REASON="$skip_reason" \
  EXIT_CODE="$exit_code" \
  FAILURE_CATEGORY="$failure_category" \
  COMMAND_STR="$command_str" \
  PYTHON_BIN="$python_bin" \
  DECISION_REASON="$decision_reason" \
  PYRIGHT_LEVEL="$pyright_level" \
  INSTALL_ATTEMPTED="$install_attempted" \
  INSTALL_COMMAND="$install_cmd" \
  INSTALL_RC="$install_rc" \
  "$sys_py" - <<'PY'
import json
import os
import pathlib
import subprocess

stage_dir = pathlib.Path(os.environ["STAGE_DIR"])
log_file = stage_dir / "log.txt"
analysis_json = stage_dir / "analysis.json"
results_json = stage_dir / "results.json"
repo = stage_dir.parent.parent

def git_commit(root: pathlib.Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return ""

def tail(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

metrics = {}
try:
    if analysis_json.exists():
        data = json.loads(analysis_json.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("metrics"), dict):
            metrics = data["metrics"]
except Exception:
    metrics = {}

install_attempted = False
try:
    install_attempted = bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0"))
except Exception:
    install_attempted = False

payload = {
    "status": os.environ.get("STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("EXIT_CODE", "1") or "1"),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_BIN", ""),
        "git_commit": git_commit(repo),
        "env_vars": {
            k: os.environ.get(k, "")
            for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"]
            if os.environ.get(k)
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_level": os.environ.get("PYRIGHT_LEVEL", "error"),
        "install_attempted": install_attempted,
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "install_returncode": os.environ.get("INSTALL_RC", ""),
        "metrics": metrics,
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(log_file),
}

results_json.parent.mkdir(parents=True, exist_ok=True)
tmp = results_json.with_suffix(results_json.suffix + ".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(results_json)
PY
}

trap 'write_results' EXIT

{
  echo "== pyright stage =="
  echo "repo: $repo"
  echo "stage_dir: $stage_dir"
  echo "mode: $mode"
  echo "level: $pyright_level"
} >"$log_file"

cd "$repo"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    auto)
      if [[ -z "$sys_py" ]]; then
        failure_category="deps"
        decision_reason="python/python3 not found on PATH to resolve report python."
        exit_code=1
        status="failure"
        exit 1
      fi
      if resolved="$("$sys_py" benchmark_scripts/runner.py --stage pyright --task check --print-python 2>>"$log_file")"; then
        python_bin="$resolved"
        py_cmd=("$python_bin")
        decision_reason="Resolved python via runner.py from report.json."
      else
        failure_category="missing_report"
        decision_reason="Could not resolve python from report.json (see log)."
        exit_code=1
        status="failure"
        printf '%s\n' "Failed to resolve python via runner.py." >>"$log_file"
        exit 1
      fi
      ;;
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >>"$log_file"; failure_category="args_unknown"; exit_code=1; status="failure"; exit 1; }
      python_bin="$venv_dir/bin/python"
      py_cmd=("$python_bin")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      python_bin="$venv_dir/bin/python"
      py_cmd=("$python_bin")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >>"$log_file"; failure_category="args_unknown"; exit_code=1; status="failure"; exit 1; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >>"$log_file"; failure_category="deps"; exit_code=1; status="failure"; exit 1; }
      python_bin="conda run -n $conda_env python"
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >>"$log_file"; failure_category="deps"; exit_code=1; status="failure"; exit 1; }
      python_bin="poetry run python"
      py_cmd=(poetry run python)
      ;;
    system)
      python_bin="$(command -v python3 || command -v python || true)"
      [[ -n "$python_bin" ]] || { echo "python/python3 not found in PATH" >>"$log_file"; failure_category="deps"; exit_code=1; status="failure"; exit 1; }
      py_cmd=("$python_bin")
      ;;
    *)
      echo "Unknown --mode: $mode" >>"$log_file"
      failure_category="args_unknown"
      exit_code=1
      status="failure"
      exit 1
      ;;
  esac
fi

echo "python_cmd: ${py_cmd[*]}" >>"$log_file"
if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_file" 2>&1; then
  failure_category="deps"
  status="failure"
  exit_code=1
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_file" 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "Installing pyright: $install_cmd" >>"$log_file"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright >>"$log_file" 2>&1
  install_rc="$?"
  set -e
  if [[ "$install_rc" != "0" ]]; then
    # Heuristic classification
    if grep -E "Temporary failure|Name or service not known|Connection|timed out|No route to host|SSL|CERTIFICATE" "$log_file" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    status="failure"
    exit_code=1
    printf '%s\n' "{}" >"$pyright_out_json"
    printf '%s\n' "{}" >"$analysis_json"
    exit 1
  fi
fi

targets=()
pyright_args=()
decision_reason="${decision_reason:-}"

if [[ -f "pyrightconfig.json" ]]; then
  pyright_args+=(--project pyrightconfig.json)
  decision_reason="${decision_reason} Using pyrightconfig.json."
elif [[ -f "pyproject.toml" ]] && grep -E '^\[tool\.pyright\]' pyproject.toml >/dev/null 2>&1; then
  pyright_args+=(--project pyproject.toml)
  decision_reason="${decision_reason} Using [tool.pyright] in pyproject.toml."
elif [[ -d "src" ]]; then
  targets+=(src)
  [[ -d "tests" ]] && targets+=(tests)
  decision_reason="${decision_reason} Using src/ layout targets."
else
  # Fallback: detect package dirs via __init__.py and keep <= depth 3 for runtime.
  mapfile -t pkgs < <(
    find . \
      -type d \( -name .git -o -name build_output -o -name benchmark_assets -o -name benchmark_scripts -o -name .venv -o -name venv -o -name node_modules -o -name dist -o -name build \) -prune -o \
      -name "__init__.py" -print 2>/dev/null \
      | sed 's|^\./||' \
      | sed 's|/[^/]*$||' \
      | awk -F/ 'NF<=3 {print $0}' \
      | sort -u
  )
  if [[ "${#pkgs[@]}" -gt 0 ]]; then
    targets+=("${pkgs[@]}")
    decision_reason="${decision_reason} Detected package dirs via __init__.py."
  fi
fi

if [[ "${#targets[@]}" -eq 0 && "${#pyright_args[@]}" -eq 0 ]]; then
  status="failure"
  exit_code=1
  failure_category="entrypoint_not_found"
  decision_reason="${decision_reason} No pyright config, src/, or package dirs found."
  printf '%s\n' "{}" >"$pyright_out_json"
  printf '%s\n' "{}" >"$analysis_json"
  exit 1
fi

# Include top-level scripts if present.
for f in app.py run.py run_video.py; do
  [[ -f "$f" ]] && targets+=("$f")
done

cmd=("${py_cmd[@]}" -m pyright)
cmd+=("${targets[@]}")
cmd+=(--level "$pyright_level" --outputjson)
cmd+=("${pyright_args[@]}")
cmd+=("${pyright_extra_args[@]}")

command_str="${cmd[*]}"
echo "Running: $command_str" >>"$log_file"

set +e
"${cmd[@]}" >"$pyright_out_json" 2>>"$log_file"
pyright_rc="$?"
set -e
echo "pyright_exit_code: $pyright_rc (ignored for stage success)" >>"$log_file"

# Post-process output to missing-import metrics. Fail only if JSON is unreadable.
set +e
PY_CMD_STR="${py_cmd[*]}" MODE="$mode" INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_cmd" \
  "$sys_py" - <<'PY' >>"$log_file" 2>&1
import ast
import json
import os
import pathlib
import re
from typing import Iterable

repo_root = pathlib.Path(".").resolve()
stage_dir = repo_root / "build_output" / "pyright"
out_json = stage_dir / "pyright_output.json"
analysis_json = stage_dir / "analysis.json"

pyright_json_valid = True
try:
    raw = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    raw = {"_error": f"pyright_output.json is not valid JSON: {e}"}
    pyright_json_valid = False

diagnostics = raw.get("generalDiagnostics", []) if isinstance(raw, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted({m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))})

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

payload = {
    "missing_packages": missing_packages,
    "pyright": raw,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD_STR", ""),
        "files_scanned": files_scanned,
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_json_valid": pyright_json_valid,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
if not pyright_json_valid:
    raise SystemExit(2)
PY
post_rc="$?"
set -e

if [[ "$post_rc" != "0" ]]; then
  status="failure"
  exit_code=1
  failure_category="invalid_json"
else
  # Pyright output parsed successfully; stage succeeds even if missing imports exist.
  status="success"
  exit_code=0
  failure_category="unknown"
fi
