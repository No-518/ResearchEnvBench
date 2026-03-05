#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report missing-import diagnostics only.

Defaults:
  - Uses python from /opt/scimlopsbench/report.json (via runner.py resolution)
  - Writes outputs to build_output/pyright/

Environment selection (overrides report python; pick one):
  --python <path>                       Explicit python executable
  --mode venv   --venv <path>           Use <venv>/bin/python
  --mode uv    [--venv <path>]          Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <name>      Use: conda run -n <name> python
  --mode poetry                         Use: poetry run python
  --mode system                         Use: python from PATH

Optional:
  --level <error|warning|information>   Default: error
  -- <pyright args...>                  Extra args passed to Pyright
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

mode=""
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
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

stage_dir="build_output/pyright"
mkdir -p "$stage_dir"

log_txt="$stage_dir/log.txt"
pyright_out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

exec >"$log_txt" 2>&1

echo "[pyright] repo_root=$repo_root"
echo "[pyright] start"

py_cmd=()
python_resolution="unknown"

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli:--python"
elif [[ -n "$mode" ]]; then
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv"; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      python_resolution="mode:venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution="mode:uv"
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda"; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH"; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_resolution="mode:conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH"; exit 2; }
      py_cmd=(poetry run python)
      python_resolution="mode:poetry"
      ;;
    system)
      py_cmd=(python)
      python_resolution="mode:system"
      ;;
    *)
      echo "Unknown --mode: $mode"
      exit 2
      ;;
  esac
else
  # Default: resolve from agent report (runner.py rules)
  if py_bin="$(python3 benchmark_scripts/runner.py resolve-python 2>/dev/null)"; then
    py_cmd=("$py_bin")
    python_resolution="report:python_path"
  else
    # Spec requirement: if report is missing/invalid and --python not provided, this stage must fail.
    py_cmd=(python3)
    python_resolution="failure:missing_report"
    status="failure"
    failure_category="missing_report"
    skip_reason="unknown"
    exit_code=1
  fi
fi

echo "[pyright] python_resolution=$python_resolution"
echo "[pyright] python_cmd=${py_cmd[*]}"

git_commit=""
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

status="${status:-failure}"
failure_category="${failure_category:-unknown}"
skip_reason="${skip_reason:-unknown}"
exit_code="${exit_code:-1}"
install_attempted=0
install_command=""
pyright_exit=0
command_str=""

echo "{}" > "$pyright_out_json"

if [[ "$failure_category" == "unknown" ]]; then
  if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    echo "[pyright] ERROR: failed to run python via: ${py_cmd[*]}"
    failure_category="deps"
  else
    if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
      install_attempted=1
      install_command="${py_cmd[*]} -m pip install -q pyright"
      echo "[pyright] pyright not importable; attempting install: $install_command"
      if ! "${py_cmd[@]}" -m pip install -q pyright; then
        echo "[pyright] ERROR: failed to install pyright"
        failure_category="download_failed"
      fi
    fi
  fi
fi

# Decide pyright targets / project selection.
project_args=()
targets=()

if [[ -f pyrightconfig.json ]]; then
  project_args=(--project pyrightconfig.json)
  targets=(".")
  echo "[pyright] using pyrightconfig.json"
elif [[ -f pyproject.toml ]] && { command -v rg >/dev/null 2>&1 && rg -n "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml >/dev/null 2>&1; } || { command -v rg >/dev/null 2>&1 || grep -qE "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml; }; then
  project_args=(--project pyproject.toml)
  targets=(".")
  echo "[pyright] using pyproject.toml [tool.pyright]"
elif [[ -d src ]]; then
  targets=("src")
  [[ -d tests ]] && targets+=("tests")
  echo "[pyright] using src/ layout targets: ${targets[*]}"
else
  # Detect top-level package dirs by presence of any __init__.py under them.
  mapfile -t detected < <(
    find . -maxdepth 4 -type f -name "__init__.py" -print \
      | awk -F/ '{print $2}' \
      | sort -u
  )
  for d in "${detected[@]:-}"; do
    case "$d" in
      ""|"."|".git"|"build_output"|"benchmark_scripts"|"benchmark_assets"|"__pycache__"|".venv"|"venv"|"dist"|"build"|"node_modules")
        continue
        ;;
    esac
    [[ -d "$d" ]] && targets+=("$d")
  done
  if [[ ${#targets[@]} -eq 0 ]]; then
    echo "[pyright] ERROR: could not determine pyright targets (no config/src/packages found)"
    failure_category="entrypoint_not_found"
  else
    echo "[pyright] detected package targets: ${targets[*]}"
  fi
fi

if [[ "$failure_category" == "unknown" ]] && [[ ${#targets[@]} -gt 0 ]]; then
  command_str="${py_cmd[*]} -m pyright --outputjson --level ${pyright_level} ${project_args[*]} ${targets[*]} ${pyright_extra_args[*]}"
  echo "[pyright] running: $command_str"

  set +e
  "${py_cmd[@]}" -m pyright --outputjson --level "$pyright_level" "${project_args[@]}" "${targets[@]}" "${pyright_extra_args[@]}" >"$pyright_out_json"
  pyright_exit=$?
  set -e

  echo "[pyright] pyright_exit=$pyright_exit"

  # Parse pyright output; success does NOT depend on pyright_exit (it may be non-zero when issues exist).
  if "${py_cmd[@]}" - <<'PY'
import json
import pathlib
import sys

path = pathlib.Path("build_output/pyright/pyright_output.json")
try:
    json.loads(path.read_text(encoding="utf-8"))
except Exception as e:
    print(f"[pyright] ERROR: invalid pyright_output.json: {e}", file=sys.stderr)
    raise
PY
  then
    status="success"
    skip_reason="not_applicable"
    exit_code=0
    failure_category="unknown"
  else
    status="failure"
    skip_reason="unknown"
    exit_code=1
    failure_category="invalid_json"
  fi
fi

# Always write analysis.json and results.json (even on failure).
OUT_JSON="$pyright_out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
MODE="${mode:-report}" PY_CMD="${py_cmd[*]}" PYRIGHT_EXIT="$pyright_exit" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" \
PYTHON_RESOLUTION="$python_resolution" GIT_COMMIT="$git_commit" \
STATUS="$status" FAILURE_CATEGORY="$failure_category" SKIP_REASON="$skip_reason" EXIT_CODE="$exit_code" \
COMMAND_STR="$command_str" PYRIGHT_LEVEL="$pyright_level" TARGETS="${targets[*]:-}" \
"${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import sys
from typing import Iterable, Set

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

status = os.environ.get("STATUS", "failure")
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown")
skip_reason = os.environ.get("SKIP_REASON", "unknown")
exit_code = int(os.environ.get("EXIT_CODE", "1"))

command_str = os.environ.get("COMMAND_STR", "")

pyright_data = {}
pyright_parse_error = None
try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    pyright_parse_error = str(e)

diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import\\s+\\"([^\\"]+)\\"')
missing_packages = sorted(
    {pattern.search(d.get("message", "")).group(1).split(".")[0] for d in missing_diags if pattern.search(d.get("message", ""))}
)

targets_str = os.environ.get("TARGETS", "").strip()
targets = [t for t in targets_str.split() if t]

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

def collect_imported_packages(py_file: pathlib.Path) -> Set[str]:
    pkgs: Set[str] = set()
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

all_imported_packages: Set[str] = set()
files_scanned = 0

repo_root = pathlib.Path(".").resolve()
scan_roots = [repo_root / t for t in targets] if targets else [repo_root]
for root in scan_roots:
    if not root.exists():
        continue
    for py_file in iter_py_files(root):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": pyright_data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
        "pyright_exit": int(os.environ.get("PYRIGHT_EXIT", "0")),
        "pyright_level": os.environ.get("PYRIGHT_LEVEL", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "") == "1",
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "targets": targets,
        "files_scanned": files_scanned,
        "pyright_parse_error": pyright_parse_error,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": status,
    "skip_reason": skip_reason,
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": command_str,
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": sys.executable,
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": (
            "Pyright targets resolved by priority: pyrightconfig.json > pyproject [tool.pyright] > src/ > package dirs. "
            "Reported metrics only for reportMissingImports diagnostics."
        ),
        "pyright": analysis_payload["meta"],
    },
    "failure_category": failure_category,
    "error_excerpt": "",
    # Stage-specific metrics (required by summarize_results.py aggregation).
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

echo "[pyright] done status=$status exit_code=$exit_code"
exit "$exit_code"
