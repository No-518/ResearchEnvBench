#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule == reportMissingImports).

Outputs (always written):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Python selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Defaults:
  If no --python/--mode is provided, uses python_path from the agent report:
    $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json

Optional:
  --repo <path>                  Repo root (default: parent of benchmark_scripts/)
  --report-path <path>           Agent report.json path (overrides SCIMLOPSBENCH_REPORT)
  --wait-report-sec <seconds>    Wait for report.json to appear/readable (default: 0)
  --level <error|warning|...>    Default: error
  -- <extra pyright args...>     Passed to pyright (e.g. --pythonpath .)
EOF
}

repo=""
mode=""
python_bin=""
venv_dir=""
conda_env=""
pyright_level="error"
pyright_extra_args=()
report_path_cli=""
wait_report_sec="${SCIMLOPSBENCH_REPORT_WAIT_SEC:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --report-path) report_path_cli="${2:-}"; shift 2 ;;
    --wait-report-sec) wait_report_sec="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pyright_extra_args=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="${repo:-"$(cd "$script_dir/.." && pwd)"}"
stage_dir="$repo/build_output/pyright"
mkdir -p "$stage_dir"

log_path="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

mkdir -p "$repo/build_output" "$repo/build_output/pyright"

# Redirect all stage logs to log.txt (the pyright JSON itself is written to pyright_output.json).
exec >"$log_path" 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] utc_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

sys_py="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_py" ]]; then
  echo "[pyright] ERROR: python3/python not found in PATH (needed to read report.json / write results)." >&2
  printf '{}' >"$out_json"
  printf '{}' >"$analysis_json"
  printf '{}' >"$results_json"
  exit 1
fi
echo "[pyright] sys_py=$sys_py"
echo "[pyright] id=$(id || true)"

report_path="${report_path_cli:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
echo "[pyright] report_path=$report_path"

py_cmd=()
python_source="unknown"
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_source="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("${SCIMLOPSBENCH_PYTHON}")
  python_source="env"
else
  case "${mode:-}" in
    venv)
      if [[ -z "${venv_dir:-}" ]]; then
        echo "[pyright] ERROR: --venv is required for --mode venv" >&2
        py_cmd=()
      else
        py_cmd=("$venv_dir/bin/python")
        python_source="venv"
      fi
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_source="uv"
      ;;
    conda)
      if [[ -z "${conda_env:-}" ]]; then
        echo "[pyright] ERROR: --conda-env is required for --mode conda" >&2
        py_cmd=()
      else
        if ! command -v conda >/dev/null 2>&1; then
          echo "[pyright] ERROR: conda not found in PATH" >&2
          py_cmd=()
        else
          py_cmd=(conda run -n "$conda_env" python)
          python_source="conda"
        fi
      fi
      ;;
    poetry)
      if ! command -v poetry >/dev/null 2>&1; then
        echo "[pyright] ERROR: poetry not found in PATH" >&2
        py_cmd=()
      else
        py_cmd=(poetry run python)
        python_source="poetry"
      fi
      ;;
	    system)
	      py_cmd=(python)
	      python_source="system"
	      ;;
	    ""|report)
	      if [[ -e "$report_path" ]]; then
	        ls -la "$report_path" || true
	      fi
	      if [[ ! -r "$report_path" ]]; then
	        # The report may be created by an external environment setup step; allow a short wait.
	        if [[ "$wait_report_sec" =~ ^[0-9]+$ ]] && [[ "$wait_report_sec" -gt 0 ]]; then
	          echo "[pyright] waiting up to ${wait_report_sec}s for readable report: $report_path"
	          for ((i=0; i<wait_report_sec; i++)); do
	            if [[ -r "$report_path" ]]; then
	              break
	            fi
	            sleep 1
	          done
	        fi
	      fi
	      if [[ ! -r "$report_path" ]]; then
	        if [[ -e "$report_path" ]]; then
	          echo "[pyright] ERROR: report.json exists but is not readable at $report_path and no --python/--mode provided" >&2
	        else
	          echo "[pyright] ERROR: report.json not found at $report_path and no --python/--mode provided" >&2
	        fi
	        py_cmd=()
		      else
		        python_path_repr="$(
		          "$sys_py" -c 'import json,sys; p=json.load(open(sys.argv[1],"r",encoding="utf-8")).get("python_path",""); print(repr(p))' \
		            "$report_path" 2>/dev/null || true
		        )"
		        if [[ -n "$python_path_repr" ]]; then
		          echo "[pyright] report.python_path_repr=$python_path_repr"
		        fi

		        # The report file may exist but be mid-write/invalid for a brief window; retry python_path parsing.
		        wait_n=0
		        if [[ "$wait_report_sec" =~ ^[0-9]+$ ]] && [[ "$wait_report_sec" -gt 0 ]]; then
		          wait_n="$wait_report_sec"
		        fi

		        parse_code=$'import json, sys\nfrom pathlib import Path\n\npath = Path(sys.argv[1])\ntry:\n    data = json.loads(path.read_text(encoding=\"utf-8\"))\nexcept FileNotFoundError:\n    print(f\"[pyright] report.json not found at {path}\", file=sys.stderr)\n    raise SystemExit(2)\nexcept PermissionError as e:\n    print(f\"[pyright] report.json not readable at {path}: {e}\", file=sys.stderr)\n    raise SystemExit(2)\nexcept Exception as e:\n    print(f\"[pyright] report.json invalid JSON at {path}: {e}\", file=sys.stderr)\n    raise SystemExit(2)\n\npython_path = str(data.get(\"python_path\") or \"\").strip()\nif not python_path:\n    print(\"[pyright] report.json missing/empty python_path\", file=sys.stderr)\n    raise SystemExit(3)\n\nprint(python_path)\n'

		        python_from_report=""
		        for ((i=0; i<=wait_n; i++)); do
		          if [[ "$i" -eq 1 ]]; then
		            echo "[pyright] waiting up to ${wait_n}s for python_path in report: $report_path"
		          fi
		          if [[ "$i" -gt 0 ]]; then
		            sleep 1
		          fi

		          if [[ "$i" -lt "$wait_n" ]]; then
		            python_from_report="$("$sys_py" -c "$parse_code" "$report_path" 2>/dev/null || true)"
		          else
		            python_from_report="$("$sys_py" -c "$parse_code" "$report_path" || true)"
		          fi

		          if [[ -n "$python_from_report" ]]; then
		            break
		          fi
	        done

	        if [[ -z "$python_from_report" ]]; then
	          echo "[pyright] ERROR: failed to read python_path from report: $report_path" >&2
	          py_cmd=()
	        else
	          py_cmd=("$python_from_report")
	          python_source="report"
	        fi
	      fi
	      ;;
    *)
      echo "[pyright] ERROR: unknown --mode: $mode" >&2
      py_cmd=()
      ;;
  esac
fi

if [[ "${#py_cmd[@]}" -eq 0 ]]; then
  echo "[pyright] ERROR: could not resolve a python interpreter." >&2
  printf '{}' >"$out_json"
  printf '{}' >"$analysis_json"
  REPO="$repo" STAGE_DIR="$stage_dir" "$sys_py" - <<'PY' || true
import json, os, subprocess, sys, time
from pathlib import Path

repo = Path(os.environ["REPO"])
stage_dir = Path(os.environ["STAGE_DIR"])
log_path = stage_dir / "log.txt"
results_path = stage_dir / "results.json"

def git_commit() -> str:
  try:
    return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()
  except Exception:
    return ""

err_excerpt = ""
try:
  err_excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
except Exception:
  pass

payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
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
    "python": sys.executable,
    "git_commit": git_commit(),
    "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT","")},
    "decision_reason": "Failed to resolve python interpreter for Pyright stage.",
  },
  "failure_category": "missing_report",
  "error_excerpt": err_excerpt,
}
results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
  exit 1
fi

python_exe="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_ver="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

echo "[pyright] python_source=$python_source"
echo "[pyright] python_cmd=${py_cmd[*]}"
echo "[pyright] python_exe=$python_exe"
echo "[pyright] python_ver=$python_ver"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] ERROR: failed to run python via: ${py_cmd[*]}" >&2
  printf '{}' >"$out_json"
fi

# Detect project/targets (do not blindly run on ".")
cd "$repo"
pyright_args=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  pyright_args=(--project pyrightconfig.json)
  decision_reason="Found pyrightconfig.json; running pyright with --project pyrightconfig.json."
elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  pyright_args=(--project pyproject.toml)
  decision_reason="Found [tool.pyright] in pyproject.toml; running pyright with --project pyproject.toml."
elif [[ -d "src" ]]; then
  pyright_args=(src)
  if [[ -d "tests" ]]; then
    pyright_args+=(tests)
  fi
  decision_reason="Detected src/ layout; running pyright on src/ (and tests/ if present)."
else
  mapfile -t pkg_dirs < <(
    find . -maxdepth 3 -type f -name '__init__.py' \
      -not -path './.git/*' \
      -not -path './.venv/*' \
      -not -path './venv/*' \
      -not -path './build_output/*' \
      -not -path './benchmark_assets/*' \
      -not -path './benchmark_scripts/*' \
      -print \
	      | sed 's|^\./||' \
	      | awk -F/ '{print $1}' \
	      | sort -u
	  )
  if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
    pyright_args=("${pkg_dirs[@]}")
    decision_reason="Detected package dirs via __init__.py; running pyright on: ${pkg_dirs[*]}."
  fi
fi

pyright_targets_json="$("$sys_py" - <<PY
import json, os, sys
print(json.dumps(os.environ.get("PYRIGHT_ARGS","").split("\\n") if os.environ.get("PYRIGHT_ARGS") else []))
PY
)"

fatal_error=0
failure_category="unknown"
install_attempted=0
install_cmd=""
install_ok=0

if [[ "${#pyright_args[@]}" -eq 0 ]]; then
  echo "[pyright] ERROR: could not determine Pyright targets/project (no src/, no packages, no pyright config)." >&2
  fatal_error=1
  failure_category="entrypoint_not_found"
  printf '{}' >"$out_json"
else
  if [[ ! -f "$out_json" ]]; then
    printf '{}' >"$out_json"
  fi

	  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
	    install_attempted=1
	    install_cmd="${py_cmd[*]} -m pip install -q pyright"
	    echo "[pyright] pyright not importable; attempting install: $install_cmd"
	    if ! "${py_cmd[@]}" -m pip --version >/dev/null 2>&1; then
	      echo "[pyright] pip is not available in this interpreter; attempting bootstrap: ${py_cmd[*]} -m ensurepip --upgrade"
	      "${py_cmd[@]}" -m ensurepip --upgrade || echo "[pyright] WARN: ensurepip failed (pip may still be unavailable)." >&2
	    fi
	    if "${py_cmd[@]}" -m pip install -q pyright; then
	      install_ok=1
	    else
	      echo "[pyright] ERROR: failed to install pyright via pip." >&2
	      fatal_error=1
      # Best-effort categorization: pip failures are deps unless clearly network-related.
      failure_category="deps"
    fi
  fi

  if [[ "$fatal_error" -eq 0 ]]; then
    # Always produce JSON output even if Pyright reports issues (non-zero exit code).
    pyright_rc=0
    "${py_cmd[@]}" -m pyright "${pyright_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json" || pyright_rc=$?
    echo "[pyright] pyright_return_code=$pyright_rc (ignored for stage success as long as output JSON exists)"
  fi
fi

if [[ ! -s "$out_json" ]]; then
  echo "[pyright] ERROR: pyright_output.json was not created or is empty." >&2
  fatal_error=1
  failure_category="${failure_category:-runtime}"
  printf '{}' >"$out_json"
fi

# Always attempt to write analysis.json + results.json (even on fatal_error).
analyze_py="$stage_dir/analyze_pyright_impl.py"
cat >"$analyze_py" <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import traceback
from typing import Iterable


repo = pathlib.Path(os.environ["REPO"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_PATH"])


def git_commit() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True, stderr=subprocess.DEVNULL
            ).strip()
        )
    except Exception:
        return ""


def tail_log(max_lines: int = 220) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:]).strip()
    except Exception:
        return ""


def write_json(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def main() -> int:
    try:
        pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception as e:
        pyright_data = {"_error": f"invalid_pyright_output_json: {e}"}

    diagnostics = (
        pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
    )
    missing_diags = [
        d
        for d in diagnostics
        if isinstance(d, dict) and d.get("rule") == "reportMissingImports"
    ]

    pattern = re.compile(r'Import "([^"]+)"')
    missing_packages = sorted(
        {
            (m.group(1).split(".")[0] if m else "")
            for d in missing_diags
            if (m := pattern.search(d.get("message", "")))
        }
    )
    missing_packages = [p for p in missing_packages if p]

    all_imported_packages = set()
    files_scanned = 0
    for py_file in iter_py_files(repo):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

    missing_packages_count = int(len(missing_packages))
    total_imported_packages_count = int(len(all_imported_packages))
    missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

    install_attempted = os.environ.get("INSTALL_ATTEMPTED", "0") == "1"
    install_ok = os.environ.get("INSTALL_OK", "0") == "1"
    fatal_error = os.environ.get("FATAL_ERROR", "0") == "1"
    failure_category = os.environ.get("FAILURE_CATEGORY", "unknown") or "unknown"

    status = "success"
    exit_code = 0
    skip_reason = "unknown"
    if fatal_error:
        status = "failure"
        exit_code = 1

    analysis_payload = {
        "missing_packages": missing_packages,
        "pyright": pyright_data,
        "meta": {
            "mode": os.environ.get("PY_SOURCE", ""),
            "python_cmd": os.environ.get("PY_CMD_STR", ""),
            "python_exe": os.environ.get("PY_EXE", ""),
            "python_version": os.environ.get("PY_VER", ""),
            "files_scanned": files_scanned,
            "install_attempted": install_attempted,
            "install_cmd": os.environ.get("INSTALL_CMD", ""),
            "install_ok": install_ok,
        },
        "metrics": {
            "missing_packages_count": missing_packages_count,
            "total_imported_packages_count": total_imported_packages_count,
            "missing_package_ratio": missing_package_ratio,
        },
    }

    write_json(analysis_json, analysis_payload)

    results_payload = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "pyright",
        "task": "check",
        "command": os.environ.get(
            "PYRIGHT_COMMAND", f"{os.environ.get('PY_CMD_STR','python')} -m pyright"
        ),
        "timeout_sec": 600,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
        "meta": {
            "python": f"{os.environ.get('PY_EXE','')} ({os.environ.get('PY_VER','')})",
            "git_commit": git_commit(),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            },
            "decision_reason": os.environ.get("DECISION_REASON", ""),
            "pyright": {
                "level": os.environ.get("PYRIGHT_LEVEL", ""),
                "targets_or_project": os.environ.get("PYRIGHT_ARGS_JOINED", "").splitlines(),
                "install_attempted": install_attempted,
                "install_cmd": os.environ.get("INSTALL_CMD", ""),
                "install_ok": install_ok,
            },
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": tail_log(),
    }

    write_json(results_json, results_payload)
    print(missing_package_ratio)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        try:
            failure_payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "pyright",
                "task": "check",
                "command": os.environ.get("PYRIGHT_COMMAND", ""),
                "timeout_sec": 600,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "meta": {
                    "python": os.environ.get("PY_EXE", ""),
                    "git_commit": git_commit(),
                    "decision_reason": os.environ.get("DECISION_REASON", ""),
                    "exception": str(e),
                    "traceback": traceback.format_exc(),
                },
                "failure_category": "unknown",
                "error_excerpt": tail_log(),
            }
            write_json(results_json, failure_payload)
        except Exception:
            pass
        raise
PY

PY_CMD_STR="${py_cmd[*]}" PYRIGHT_LEVEL="$pyright_level" PYRIGHT_ARGS_JOINED="$(printf '%s\n' "${pyright_args[@]}")" \
PYRIGHT_COMMAND="${py_cmd[*]} -m pyright ${pyright_args[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}" \
DECISION_REASON="$decision_reason" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" INSTALL_OK="$install_ok" \
FATAL_ERROR="$fatal_error" FAILURE_CATEGORY="$failure_category" PY_SOURCE="$python_source" PY_EXE="$python_exe" PY_VER="$python_ver" \
REPO="$repo" OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_PATH="$log_path" \
"${py_cmd[@]}" "$analyze_py" || echo "[pyright] ERROR: analysis step failed" >&2

if [[ ! -f "$analysis_json" || ! -f "$results_json" ]]; then
  echo "[pyright] ERROR: analysis.json/results.json were not written." >&2
  exit 1
fi

status="$(
  "$sys_py" - "$results_json" 2>/dev/null <<'PY'
import json, sys
try:
  with open(sys.argv[1], "r", encoding="utf-8") as f:
    d = json.load(f)
  print(d.get("status",""))
except Exception:
  print("")
PY
)"

if [[ "$status" == "success" || "$status" == "skipped" ]]; then
  exit 0
fi
exit 1
