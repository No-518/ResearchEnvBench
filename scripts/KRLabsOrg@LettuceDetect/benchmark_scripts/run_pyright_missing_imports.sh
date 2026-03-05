#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (default under build_output/pyright/):
  log.txt
  pyright_output.json   # raw Pyright JSON output
  analysis.json         # {missing_packages, pyright, meta, metrics}
  results.json          # benchmark stage results + pyright metrics

Required:
  --repo <path>         Path to the repository/project to analyze

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report.json (required unless --python/SCIMLOPSBENCH_PYTHON)

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright

Examples:
  ./benchmark_scripts/run_pyright_missing_imports.sh --repo . --mode system
  ./benchmark_scripts/run_pyright_missing_imports.sh --repo . --python /abs/path/to/python
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

mkdir -p "$REPO_ROOT/$out_dir"
log_path="$REPO_ROOT/$out_dir/log.txt"
out_json="$REPO_ROOT/$out_dir/pyright_output.json"
analysis_json="$REPO_ROOT/$out_dir/analysis.json"
results_json="$REPO_ROOT/$out_dir/results.json"

exec > >(tee "$log_path") 2>&1

status="failure"
failure_category="unknown"
skip_reason="unknown"
stage_exit_code=1

py_cmd=()
resolved_python_exe=""
python_source="unknown"
python_warning=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_source="cli:--python"
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      python_source="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_source="uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_source="conda:$conda_env"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_source="poetry"
      ;;
    system)
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
        python_source="env:SCIMLOPSBENCH_PYTHON"
      else
        if [[ ! -f "$report_path" ]]; then
          py_cmd=()
          python_source="report:missing"
        else
          # Resolve python_path from report.json.
          report_python="$(
            python - <<'PY' "$report_path" 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    v = data.get("python_path", "")
    print(v if isinstance(v, str) else "")
except Exception:
    print("")
PY
          )"
          if [[ -n "$report_python" && -x "$report_python" ]]; then
            py_cmd=("$report_python")
            python_source="report:python_path"
          elif [[ -n "$report_python" ]]; then
            py_cmd=(python)
            python_source="PATH:fallback"
            python_warning="report python_path is not executable; falling back to python from PATH"
          else
            py_cmd=()
            python_source="report:missing"
          fi
        fi
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "== Pyright stage =="
echo "repo=$repo"
echo "out_dir=$out_dir"
echo "python_source=$python_source"
echo "py_cmd=${py_cmd[*]}"
if [[ -n "$python_warning" ]]; then
  echo "python_warning=$python_warning"
fi

mkdir -p "$(dirname "$out_json")"
printf '%s\n' '{}' >"$out_json"

if [[ ${#py_cmd[@]} -eq 0 ]]; then
  echo "ERROR: cannot resolve python (missing/invalid report.json and no --python/SCIMLOPSBENCH_PYTHON)" >&2
  failure_category="missing_report"
  status="failure"
  python - <<'PY' \
    "$analysis_json" "$results_json" "$log_path" "${py_cmd[*]}" "$python_source" "$report_path" "$python_warning"
import json
import os
import subprocess
import sys
from pathlib import Path

analysis_json, results_json, log_path, py_cmd, python_source, report_path, python_warning = sys.argv[1:]

def tail(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:])

def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], check=False, capture_output=True, text=True)
        if cp.returncode == 0:
            return (cp.stdout or "").strip()
    except Exception:
        pass
    return ""

analysis_payload = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "python_cmd": py_cmd,
        "python_source": python_source,
        "python_warning": python_warning,
        "report_path": report_path,
        "error": "Failed to resolve python interpreter (missing/invalid report.json)",
        "install_attempted": 0,
        "install_cmd": "",
        "install_exit_code": 0,
    },
    "metrics": {
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    },
}
Path(analysis_json).write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": "<python_resolution_failed>",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    },
    "meta": {
        "python": sys.executable,
        "git_commit": git_commit(),
        "env_vars": {k: v for k, v in os.environ.items() if k in {"SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"}},
        "decision_reason": "",
        **analysis_payload["meta"],
    },
    "failure_category": "missing_report",
    "error_excerpt": tail(Path(log_path), max_lines=220),
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
}
Path(results_json).write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  failure_category="deps"
  status="failure"
  # Still emit analysis.json + results.json even if the selected python is broken.
  python - <<'PY' \
    "$analysis_json" "$results_json" "$log_path" "${py_cmd[*]}" "$python_source" "$report_path"
import json
import os
import subprocess
import sys
from pathlib import Path

analysis_json, results_json, log_path, py_cmd, python_source, report_path = sys.argv[1:]

def tail(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:])

def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], check=False, capture_output=True, text=True)
        if cp.returncode == 0:
            return (cp.stdout or "").strip()
    except Exception:
        pass
    return ""

analysis_payload = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "python_cmd": py_cmd,
        "python_source": python_source,
        "python_warning": "",
        "report_path": report_path,
        "error": "Failed to run selected python interpreter",
    },
    "metrics": {
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    },
}
Path(analysis_json).write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": "<python_unavailable>",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    },
    "meta": {
        "python": sys.executable,
        "git_commit": git_commit(),
        "env_vars": {k: v for k, v in os.environ.items() if k in {"SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"}},
        "decision_reason": "",
        **analysis_payload["meta"],
    },
    "failure_category": "deps",
    "error_excerpt": tail(Path(log_path), max_lines=220),
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
}
Path(results_json).write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
else
  resolved_python_exe="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"

  install_attempted=0
  install_cmd=""
  install_exit_code=0

	  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
	    install_attempted=1
	    install_cmd="${resolved_python_exe:-${py_cmd[*]}} -m pip install -q pyright"
	    echo "Installing pyright: $install_cmd"
	    if [[ "${SCIMLOPSBENCH_OFFLINE:-}" == "1" ]]; then
	      echo "Offline mode: setting PIP_NO_INDEX=1 for pyright install attempt"
	      export PIP_NO_INDEX=1
	      export PIP_DISABLE_PIP_VERSION_CHECK=1
	    fi
	    set +e
	    "${py_cmd[@]}" -m pip install -q pyright
	    install_exit_code=$?
	    set -e
	    if [[ $install_exit_code -ne 0 ]]; then
	      echo "pyright installation failed (exit=$install_exit_code)" >&2
	      failure_category="deps"
	      if rg -n "Temporary failure|Name or service not known|Connection|timed out|TLS|CERTIFICATE|Proxy|HTTPError" "$log_path" >/dev/null 2>&1; then
	        failure_category="download_failed"
	      fi
	      status="failure"
	    fi
	  fi

	  pyright_available=1
	  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
	    pyright_available=0
	    echo "pyright is not available after installation attempt; skipping pyright execution."
	    status="failure"
	    [[ "$failure_category" == "unknown" ]] && failure_category="deps"
	  fi

  decision_reason=""
  project_args=()
  targets=()

  cd "$repo"

  if [[ -f "pyrightconfig.json" ]]; then
    project_args=(--project pyrightconfig.json)
    targets=(".")
    decision_reason="Used pyrightconfig.json"
  elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
    project_args=(--project pyproject.toml)
    targets=(".")
    decision_reason="Used pyproject.toml [tool.pyright]"
  elif [[ -d "src" ]]; then
    targets=("src")
    [[ -d "tests" ]] && targets+=("tests")
    decision_reason="Used src/ layout targets"
  else
    # Detect python package dirs by __init__.py.
    mapfile -t pkg_dirs < <(
      find . -type f -name "__init__.py" \
        -not -path "./.git/*" \
        -not -path "./build_output/*" \
        -not -path "./benchmark_assets/*" \
        -not -path "./benchmark_scripts/*" \
        -not -path "./.venv/*" \
        -not -path "./venv/*" \
        -not -path "./dist/*" \
        -not -path "./build/*" \
        -print \
        | sed 's#/__init__\.py$##' \
        | sort -u
    )
    if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
      targets=("${pkg_dirs[@]}")
      decision_reason="Detected package directories via __init__.py"
    fi
  fi

  if [[ ${#targets[@]} -eq 0 ]]; then
    echo "No Pyright targets found (no pyrightconfig, no [tool.pyright], no src/, no package dirs)." >&2
    failure_category="entrypoint_not_found"
    status="failure"
  else
    echo "Pyright targets: ${targets[*]}"
    echo "Pyright project args: ${project_args[*]:-<none>}"
    echo "Decision: $decision_reason"

    pyright_exit_code=1
    pyright_cmd_display=""
    if [[ "${pyright_available:-0}" -eq 1 ]]; then
      pyright_exit_code=0
      pyright_cmd=("${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${project_args[@]}" "${pyright_extra_args[@]}")
      pyright_cmd_display="$(printf '%q ' "${pyright_cmd[@]}")"
      pyright_cmd_display="${pyright_cmd_display% }"
      echo "Running: ${pyright_cmd[*]}"
      set +e
      "${pyright_cmd[@]}" >"$out_json"
      pyright_exit_code=$?
      set -e
      echo "pyright_exit_code=$pyright_exit_code"

      if ! "${py_cmd[@]}" -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$out_json" >/dev/null 2>&1; then
        echo "pyright_output.json is not valid JSON" >&2
        failure_category="invalid_json"
        status="failure"
      else
        status="success"
        failure_category="unknown"
        stage_exit_code=0
      fi
    else
      echo "Skipping pyright run because pyright is unavailable."
    fi
	  fi

  # Produce analysis.json + results.json (even on failure).
  OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
    LOG_PATH="$log_path" STATUS="$status" FAILURE_CATEGORY="$failure_category" \
    PYRIGHT_LEVEL="$pyright_level" PYRIGHT_EXIT_CODE="${pyright_exit_code:-0}" \
    INSTALL_ATTEMPTED="${install_attempted:-0}" INSTALL_CMD="${install_cmd:-}" INSTALL_EXIT_CODE="${install_exit_code:-0}" \
    MODE="$mode" PY_CMD="${py_cmd[*]}" PYTHON_EXE="$resolved_python_exe" PYTHON_SOURCE="$python_source" \
    PYTHON_WARNING="$python_warning" PYRIGHT_CMD_DISPLAY="${pyright_cmd_display:-}" \
    DECISION_REASON="$decision_reason" PROJECT_ARGS="${project_args[*]:-}" TARGETS="${targets[*]:-}" \
    REPORT_PATH="$report_path" \
    "${py_cmd[@]}" - <<'PY'
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
log_path = pathlib.Path(os.environ["LOG_PATH"])

def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], check=False, capture_output=True, text=True)
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""

def tail(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])

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

pyright_payload: dict = {}
try:
    pyright_payload = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    pyright_payload = {}

diagnostics = pyright_payload.get("generalDiagnostics", []) if isinstance(pyright_payload, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

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
    "pyright": pyright_payload,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "python_executable": os.environ.get("PYTHON_EXE", ""),
        "python_source": os.environ.get("PYTHON_SOURCE", ""),
        "python_warning": os.environ.get("PYTHON_WARNING", ""),
        "files_scanned": files_scanned,
        "report_path": os.environ.get("REPORT_PATH", ""),
        "install_attempted": int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0"),
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "install_exit_code": int(os.environ.get("INSTALL_EXIT_CODE", "0") or "0"),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or "0"),
        "targets": os.environ.get("TARGETS", ""),
        "project_args": os.environ.get("PROJECT_ARGS", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

status = os.environ.get("STATUS", "failure")
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown")
exit_code = 0 if status in {"success", "skipped"} else 1

results_payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD_DISPLAY", "") or (analysis_payload["meta"].get("python_cmd", "") + " -m pyright"),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    },
    "meta": {
        "python": analysis_payload["meta"].get("python_executable", sys.executable),
        "git_commit": git_commit(),
        "env_vars": {
            k: ("***REDACTED***" if k in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "OPENAI_API_KEY"} else v)
            for k, v in os.environ.items()
            if k in {
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "HF_HUB_CACHE",
                "HUGGINGFACE_HUB_CACHE",
                "TRANSFORMERS_CACHE",
                "XDG_CACHE_HOME",
                "TORCH_HOME",
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
                "HF_TOKEN",
                "HUGGINGFACE_HUB_TOKEN",
                "OPENAI_API_KEY",
            }
        },
        "decision_reason": analysis_payload["meta"].get("decision_reason", ""),
        "pyright_exit_code": analysis_payload["meta"].get("pyright_exit_code", 0),
        "install_attempted": analysis_payload["meta"].get("install_attempted", 0),
        "install_cmd": analysis_payload["meta"].get("install_cmd", ""),
        "install_exit_code": analysis_payload["meta"].get("install_exit_code", 0),
        "python_source": analysis_payload["meta"].get("python_source", ""),
        "python_warning": analysis_payload["meta"].get("python_warning", ""),
        "targets": analysis_payload["meta"].get("targets", ""),
        "project_args": analysis_payload["meta"].get("project_args", ""),
    },
    "failure_category": failure_category,
    "error_excerpt": tail(log_path, max_lines=220),
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

  stage_exit_code="$(python - <<'PY' "$results_json" 2>/dev/null || echo 1
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(int(data.get("exit_code", 1)))
except Exception:
    print(1)
PY
  )"
fi

exit "$stage_exit_code"
