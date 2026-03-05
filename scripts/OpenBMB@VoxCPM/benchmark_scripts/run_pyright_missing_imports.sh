#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

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
  --mode system                  Use: python from PATH (default)

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <n>              Default: 600
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes pkg)

Notes:
  - Installs Pyright into the selected environment if missing:
      "<resolved_python>" -m pip install -q pyright
  - Pyright non-zero exit does NOT crash this script; JSON is still produced.
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
timeout_sec="600"
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
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

mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
: >"$log_file"

# If no explicit --python is provided, prefer SCIMLOPSBENCH_PYTHON (typically sourced from report.json).
if [[ -z "$python_bin" && -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  python_bin="${SCIMLOPSBENCH_PYTHON}"
fi

# Ensure all Python subprocesses avoid writing __pycache__ into the repo.
export PYTHONDONTWRITEBYTECODE=1

# Direct pip cache into allowed directory tree.
repo_root="$(cd "$repo" && pwd -P)"
mkdir -p "$repo_root/benchmark_assets/cache/pip"
export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1

out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then echo "--venv is required for --mode venv" | tee -a "$log_file" >&2; exit 2; fi
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then echo "--conda-env is required for --mode conda" | tee -a "$log_file" >&2; exit 2; fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" | tee -a "$log_file" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" | tee -a "$log_file" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" | tee -a "$log_file" >&2
      usage
      exit 2
      ;;
  esac
fi

cd "$repo_root" || exit 1

git_commit=""
if command -v git >/dev/null 2>&1 && git rev-parse HEAD >/dev/null 2>&1; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

command_str=""
status="failure"
failure_category="unknown"
skip_reason="not_applicable"
install_attempted=0
install_cmd=""
pyright_exit=0
pyright_env_binding=""
pyright_env_args=()
project_args=()
targets=()
decision_reason=""

detect_targets() {
  if [[ -f "pyrightconfig.json" ]]; then
    project_args=(--project "pyrightconfig.json")
    decision_reason="Detected pyrightconfig.json; running pyright with --project pyrightconfig.json."
    return 0
  fi
  if [[ -f "pyproject.toml" ]] && command -v rg >/dev/null 2>&1 && rg -n "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml >/dev/null 2>&1; then
    project_args=(--project "pyproject.toml")
    decision_reason="Detected [tool.pyright] in pyproject.toml; running pyright with --project pyproject.toml."
    return 0
  fi
  if [[ -d "src" ]]; then
    targets=("src")
    [[ -d "tests" ]] && targets+=("tests")
    [[ -d "scripts" ]] && targets+=("scripts")
    decision_reason="Detected src/ layout; running pyright on targets: ${targets[*]}."
    return 0
  fi

  # Detect package dirs containing __init__.py (top-level only).
  mapfile -t pkg_dirs < <(find . -maxdepth 2 -type f -name '__init__.py' 2>/dev/null | awk -F/ '{print $2}' | sort -u)
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="Detected package directories via __init__.py; running pyright on targets: ${targets[*]}."
    return 0
  fi

  return 1
}

{
  echo "[pyright] repo=$repo_root"
  echo "[pyright] out_dir=$out_dir"
  echo "[pyright] python_cmd=${py_cmd[*]}"
  echo "[pyright] mode=$mode"
} >>"$log_file"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_file" 2>&1; then
  echo "[pyright] ERROR: Failed to run python via: ${py_cmd[*]}" >>"$log_file"
  failure_category="deps"
  status="failure"
  pyright_exit=1
else
  if ! detect_targets; then
    echo "[pyright] ERROR: Could not detect a pyright target (no pyrightconfig.json, no [tool.pyright], no src/, no packages)." >>"$log_file"
    failure_category="entrypoint_not_found"
    status="failure"
  else
    # Preconditions satisfied unless later steps fail.
    status="success"
    failure_category="unknown"

    # Ensure pyright is importable; if not, attempt install.
    if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_file" 2>&1; then
      install_attempted=1
      install_cmd="${py_cmd[*]} -m pip install -q pyright"
      echo "[pyright] Installing pyright: $install_cmd" >>"$log_file"
      if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_file" 2>&1; then
        echo "[pyright] ERROR: pyright installation failed." >>"$log_file"
        # Heuristic categorization.
        if command -v rg >/dev/null 2>&1 && rg -i "connection|timed out|temporary failure|name or service not known" "$log_file" >/dev/null 2>&1; then
          failure_category="download_failed"
        else
          failure_category="deps"
        fi
        status="failure"
      fi
    fi

    # Only run pyright if we have not hit a hard failure.
    if [[ "$status" != "failure" ]]; then
      # Try to bind Pyright's import resolution to the selected interpreter env.
      # Different Pyright versions expose different flags, so detect from --help.
      pyright_help="$("${py_cmd[@]}" -m pyright --help 2>&1 || true)"
      if printf "%s" "$pyright_help" | grep -q -- "--pythonpath"; then
        pyright_env_args=(--pythonpath "${py_cmd[0]}")
        pyright_env_binding="--pythonpath=${py_cmd[0]}"
      else
        prefix="$("${py_cmd[@]}" -c 'import sys; print(sys.prefix)' 2>>"$log_file" || true)"
        prefix="${prefix//$'\r'/}"
        if [[ -n "$prefix" && -x "$prefix/bin/python" ]] && printf "%s" "$pyright_help" | grep -q -- "--venvpath"; then
          env_name="$(basename "$prefix")"
          env_parent="$(dirname "$prefix")"
          pyright_env_args=(--venvpath "$env_parent" --venv "$env_name")
          pyright_env_binding="--venvpath=$env_parent --venv=$env_name (sys.prefix=$prefix)"
        fi
      fi
      echo "[pyright] env_binding=${pyright_env_binding:-none}" >>"$log_file"

      command_str="${py_cmd[*]} -m pyright ${project_args[*]} ${pyright_env_args[*]} ${targets[*]} --level $pyright_level --outputjson ${pyright_extra_args[*]}"
      echo "[pyright] Running: $command_str" >>"$log_file"
      # Always capture JSON output even if pyright returns non-zero.
      "${py_cmd[@]}" -m pyright "${project_args[@]}" "${pyright_env_args[@]}" "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json" 2>>"$log_file" || pyright_exit=$?
      pyright_exit="${pyright_exit:-0}"
      echo "[pyright] pyright_exit=$pyright_exit" >>"$log_file"
    fi
  fi
fi

# Ensure pyright_output.json exists (even if pyright couldn't run).
if [[ ! -s "$out_json" ]]; then
  echo "{}" >"$out_json"
fi

# Analyze missing-import diagnostics and produce analysis/results JSON.
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
REPO_ROOT="$repo_root" MODE="$mode" PY_CMD="${py_cmd[*]}" \
GIT_COMMIT="$git_commit" DECISION_REASON="$decision_reason" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" \
PYRIGHT_EXIT="$pyright_exit" PYRIGHT_TARGETS="${targets[*]}" PYRIGHT_PROJECT_ARGS="${project_args[*]}" \
PYRIGHT_ENV_BINDING="$pyright_env_binding" \
TIMEOUT_SEC="$timeout_sec" STATUS="$status" FAILURE_CATEGORY="$failure_category" \
"${py_cmd[@]}" - <<'PY' >>"$log_file" 2>&1 || true
import ast
import json
import os
import pathlib
import re
import sys
from typing import Iterable

def safe_load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

data = safe_load_json(out_json)
diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
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
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set[str]:
    pkgs: set[str] = set()
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

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(repo_root):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

initial_status = os.environ.get("STATUS", "failure")
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown")

status = initial_status
if initial_status != "failure":
    # Treat missing-import diagnostics as a failure for this stage.
    if missing_packages_count > 0:
        status = "failure"
        failure_category = "deps"
    else:
        # If Pyright couldn't actually run (empty/invalid JSON), fail even if prechecks passed.
        if not (isinstance(data, dict) and str(data.get("version", "")).strip()):
            status = "failure"
            failure_category = "runtime"

exit_code = 0 if status in ("success", "skipped") else 1

def env_subset(keys):
    return {k: os.environ.get(k, "") for k in keys}

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "env_binding": os.environ.get("PYRIGHT_ENV_BINDING", ""),
        "files_scanned": files_scanned,
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT", "0") or 0),
        "targets": os.environ.get("PYRIGHT_TARGETS", ""),
        "project_args": os.environ.get("PYRIGHT_PROJECT_ARGS", ""),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or 0)),
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")

results_payload = {
    "status": status,
    "skip_reason": "not_applicable",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": "",
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600") or 600),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "meta": {
        "python": sys.executable,
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": env_subset(["PYTHONDONTWRITEBYTECODE", "PIP_CACHE_DIR"]),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "env_binding": os.environ.get("PYRIGHT_ENV_BINDING", ""),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or 0)),
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT", "0") or 0),
    },
    "failure_category": failure_category if status == "failure" else "unknown",
    "error_excerpt": "",
}
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(missing_package_ratio)
PY

# If results.json wasn't produced (e.g., analysis failed), write a minimal failure results.json.
if [[ ! -s "$results_json" ]]; then
  python - <<PY >>"$log_file" 2>&1 || true
import json
from pathlib import Path
Path("$results_json").write_text(json.dumps({
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "$command_str",
  "timeout_sec": int("$timeout_sec"),
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "git_commit": "$git_commit", "decision_reason": "$decision_reason"},
  "failure_category": "unknown",
  "error_excerpt": ""
}, ensure_ascii=False, indent=2), encoding="utf-8")
PY
fi

# Patch in the command + error_excerpt into results.json (keep python-only json generation above).
command_str="${command_str:-}"

python - <<PY >>"$log_file" 2>&1 || true
import json, pathlib

results_path = pathlib.Path("$results_json")
log_path = pathlib.Path("$log_file")
try:
    data = json.loads(results_path.read_text(encoding="utf-8"))
except Exception:
    data = {}

def tail(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

data["command"] = "$command_str"
data["error_excerpt"] = tail(log_path)
results_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY

exit_code="$(python - <<PY 2>/dev/null || echo 1
import json
from pathlib import Path
d=json.loads(Path("$results_json").read_text(encoding="utf-8"))
print(int(d.get("exit_code", 1)))
PY
)"
exit "$exit_code"
