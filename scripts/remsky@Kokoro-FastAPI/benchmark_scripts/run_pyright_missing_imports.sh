#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection (pick ONE):
  --python <cmd>                 Explicit python command or path (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH (default)

Optional:
  --repo <path>                  Repo root to analyze (default: auto-detect)
  --level <error|warning|...>    Default: error
  --timeout-sec <n>              Recorded in results.json only (default: 600)
  -- <pyright args...>           Extra args passed to Pyright
EOF
}

mode="system"
repo=""
pyright_level="error"
python_arg=""
venv_dir=""
conda_env=""
timeout_sec="600"
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_arg="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-600}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pyright_extra_args=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
repo="${repo:-$REPO_ROOT}"

OUT_DIR="$REPO_ROOT/build_output/pyright"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/log.txt"
OUT_JSON="$OUT_DIR/pyright_output.json"
ANALYSIS_JSON="$OUT_DIR/analysis.json"
RESULTS_JSON="$OUT_DIR/results.json"

# Capture *all* output for this stage into log.txt.
exec > >(tee "$LOG_FILE") 2>&1

cd "$repo"

py_cmd=()
python_resolution_source=""
python_resolution_warning=""

# Avoid writing __pycache__ into the repository.
export PYTHONDONTWRITEBYTECODE=1

if [[ -n "$python_arg" ]]; then
  # Accept either a path or a command string.
  # shellcheck disable=SC2206
  py_cmd=($python_arg)
  python_resolution_source="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  # shellcheck disable=SC2206
  py_cmd=(${SCIMLOPSBENCH_PYTHON})
  python_resolution_source="env"
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        python_resolution_source="args"
        py_cmd=(python)
      else
        py_cmd=("$venv_dir/bin/python")
        python_resolution_source="venv"
      fi
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution_source="uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        python_resolution_source="args"
        py_cmd=(python)
      else
        if ! command -v conda >/dev/null 2>&1; then
          echo "conda not found in PATH" >&2
          python_resolution_source="deps"
          py_cmd=(python)
        else
          py_cmd=(conda run -n "$conda_env" python)
          python_resolution_source="conda"
        fi
      fi
      ;;
    poetry)
      if ! command -v poetry >/dev/null 2>&1; then
        echo "poetry not found in PATH" >&2
        python_resolution_source="deps"
        py_cmd=(python)
      else
        py_cmd=(poetry run python)
        python_resolution_source="poetry"
      fi
      ;;
    system)
      py_cmd=(python)
      python_resolution_source="system"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

python_cmd_str="${py_cmd[*]}"

stage_status="success"
failure_category=""
install_attempted=0
install_cmd=""
install_ok=0
pyright_exit_code=0
writer_py_cmd=("${py_cmd[@]}")

echo "Repo: $repo"
echo "Python cmd: $python_cmd_str"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  stage_status="failure"
  failure_category="deps"
  # Fall back to a best-effort system python for writing JSON outputs.
  if command -v python3 >/dev/null 2>&1; then
    writer_py_cmd=(python3)
  elif command -v python >/dev/null 2>&1; then
    writer_py_cmd=(python)
  fi
fi

project_args=()
targets=()
decision_reason=""

if [[ "$stage_status" == "success" ]]; then
  if [[ -f "pyrightconfig.json" ]]; then
    project_args=(--project "pyrightconfig.json")
    decision_reason="Using pyrightconfig.json"
  elif [[ -f "pyproject.toml" ]]; then
    if "${py_cmd[@]}" - <<-'PY' >/dev/null 2>&1; then
import sys
try:
    import tomllib  # py>=3.11
except Exception:
    try:
        import tomli as tomllib  # type: ignore
    except Exception:
        sys.exit(2)

from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
tool = data.get("tool", {})
	print("ok" if "pyright" in tool else "no")
	sys.exit(0 if "pyright" in tool else 1)
	PY
	      project_args=(--project "pyproject.toml")
	      decision_reason="Using [tool.pyright] in pyproject.toml"
	    fi
	  fi

  if [[ ${#project_args[@]} -eq 0 ]]; then
    if [[ -d "src" ]]; then
      targets=("src")
      if [[ -d "tests" ]]; then
        targets+=("tests")
      fi
      decision_reason="Using src/ layout targets"
    else
      # Detect top-level package dirs by presence of __init__.py anywhere under them.
      mapfile -t found < <(
        find . \
          -type d \( -name .git -o -name .venv -o -name venv -o -name node_modules -o -name build_output -o -name benchmark_assets \) -prune -o \
          -type f -name "__init__.py" -print 2>/dev/null \
          | sed 's|^\./||' \
          | awk -F/ '{print $1}' \
          | sort -u
      )

      # Prefer commonly relevant roots if present.
      for d in api ui; do
        for f in "${found[@]:-}"; do
          if [[ "$f" == "$d" ]]; then
            targets+=("$d")
          fi
        done
      done

      # Add remaining detected top-level package dirs.
      for f in "${found[@]:-}"; do
        case "$f" in
          api|ui) : ;;
          *) targets+=("$f") ;;
        esac
      done

      if [[ ${#targets[@]} -gt 0 ]]; then
        decision_reason="Detected package dirs via __init__.py: ${targets[*]}"
      fi
    fi
  fi

  if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
    echo "Failed to auto-detect Pyright targets (no pyrightconfig, no [tool.pyright], no src/, no package dirs)." >&2
    stage_status="failure"
    failure_category="entrypoint_not_found"
    decision_reason="No analyzable Python targets detected"
  fi
fi

# Ensure pyright is available, install if missing (mandatory).
if [[ "$stage_status" == "success" ]]; then
  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
    install_attempted=1
    install_cmd="${python_cmd_str} -m pip install -q pyright"
    echo "Installing pyright: $install_cmd"
    if "${py_cmd[@]}" -m pip install -q pyright; then
      install_ok=1
    else
      echo "Failed to install pyright" >&2
      stage_status="failure"
      failure_category="deps"
    fi
  else
    install_ok=1
  fi
fi

# Always write a JSON file, even if pyright couldn't run.
echo '{}' > "$OUT_JSON"

pyright_cmd_str=""
if [[ "$stage_status" == "success" ]]; then
  pyright_cmd_str="${python_cmd_str} -m pyright"
  if [[ ${#project_args[@]} -gt 0 ]]; then
    pyright_cmd_str+=" ${project_args[*]}"
  else
    pyright_cmd_str+=" ${targets[*]}"
  fi
  pyright_cmd_str+=" --level ${pyright_level} --outputjson"
  if [[ ${#pyright_extra_args[@]} -gt 0 ]]; then
    pyright_cmd_str+=" ${pyright_extra_args[*]}"
  fi

  echo "Running: $pyright_cmd_str"
  if [[ ${#project_args[@]} -gt 0 ]]; then
    if ! "${py_cmd[@]}" -m pyright "${project_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$OUT_JSON"; then
      pyright_exit_code=$?
      echo "Pyright exited non-zero: $pyright_exit_code (continuing to produce JSON outputs)"
    fi
  else
    if ! "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$OUT_JSON"; then
      pyright_exit_code=$?
      echo "Pyright exited non-zero: $pyright_exit_code (continuing to produce JSON outputs)"
    fi
  fi
fi

# Post-process and write analysis.json + stage results.json (always).
MODE="$mode" PY_CMD_STR="$python_cmd_str" PYRIGHT_CMD_STR="$pyright_cmd_str" \
PYTHON_RESOLUTION_SOURCE="$python_resolution_source" PYTHON_RESOLUTION_WARNING="$python_resolution_warning" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" INSTALL_OK="$install_ok" \
STAGE_STATUS="$stage_status" FAILURE_CATEGORY="$failure_category" PYRIGHT_EXIT_CODE="$pyright_exit_code" \
DECISION_REASON="$decision_reason" TIMEOUT_SEC="$timeout_sec" \
OUT_JSON="$OUT_JSON" ANALYSIS_JSON="$ANALYSIS_JSON" RESULTS_JSON="$RESULTS_JSON" LOG_FILE="$LOG_FILE" \
"${writer_py_cmd[@]}" - <<'PY'
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
log_file = pathlib.Path(os.environ["LOG_FILE"])

stage_status = os.environ.get("STAGE_STATUS", "failure")
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown")
timeout_sec = int(os.environ.get("TIMEOUT_SEC", "600"))

def get_git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""

def tail_error_excerpt() -> str:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[-200:])

def iter_py_files(roots: list[pathlib.Path]) -> Iterable[pathlib.Path]:
    exclude_dirs = {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
        "build_output",
        "benchmark_assets",
        "benchmark_scripts",
    }
    for root in roots:
        if not root.exists():
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

pyright_data: dict = {}
diagnostics: list[dict] = []
try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
    diagnostics = pyright_data.get("generalDiagnostics", []) or []
except Exception:
    pyright_data = {}
    diagnostics = []

missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]
pattern = re.compile(r'Import "([^"]+)"')
missing_packages = sorted(
    {pattern.search(d.get("message", "")).group(1).split(".")[0] for d in missing_diags if pattern.search(d.get("message", ""))}
)

# Choose scan roots: prefer api/src and ui if present; else repo root.
repo_root = pathlib.Path(".").resolve()
scan_roots: list[pathlib.Path] = []
if (repo_root / "api" / "src").exists():
    scan_roots.append(repo_root / "api" / "src")
if (repo_root / "ui").exists():
    scan_roots.append(repo_root / "ui")
if not scan_roots:
    scan_roots.append(repo_root)

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(scan_roots):
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
        "python_cmd": os.environ.get("PY_CMD_STR", ""),
        "pyright_cmd": os.environ.get("PYRIGHT_CMD_STR", ""),
        "python_resolution_source": os.environ.get("PYTHON_RESOLUTION_SOURCE", ""),
        "python_resolution_warning": os.environ.get("PYTHON_RESOLUTION_WARNING", ""),
        "install_attempted": int(os.environ.get("INSTALL_ATTEMPTED", "0")),
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "install_ok": int(os.environ.get("INSTALL_OK", "0")),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0")),
        "files_scanned": files_scanned,
        "scan_roots": [str(p) for p in scan_roots],
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

exit_code = 0 if stage_status in {"success", "skipped"} else 1

# Required stage results envelope
results_payload = {
    "status": stage_status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD_STR", ""),
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "meta": {
        "python": os.environ.get("PY_CMD_STR", ""),
        "git_commit": get_git_commit(),
        "env_vars": {
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": int(os.environ.get("INSTALL_ATTEMPTED", "0")),
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "install_ok": int(os.environ.get("INSTALL_OK", "0")),
        "python_resolution_source": os.environ.get("PYTHON_RESOLUTION_SOURCE", ""),
        "python_resolution_warning": os.environ.get("PYTHON_RESOLUTION_WARNING", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0")),
        "analysis_json": str(analysis_json),
        "pyright_output_json": str(out_json),
    },
    "failure_category": failure_category if stage_status == "failure" else "",
    "error_excerpt": tail_error_excerpt(),
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
sys.exit(exit_code)
PY
