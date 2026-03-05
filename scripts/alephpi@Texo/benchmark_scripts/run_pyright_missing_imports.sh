#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Defaults:
  - Repository root is auto-detected from this script location.
  - Output dir: build_output/pyright
  - Python interpreter is resolved in this priority:
      1) --python
      2) SCIMLOPSBENCH_PYTHON
      3) /opt/scimlopsbench/report.json (or SCIMLOPSBENCH_REPORT) -> python_path
      4) Fallback: python from PATH (recorded as warning)

Environment selection:
  --python <path>                Explicit python executable to use
  --mode venv|uv|conda|poetry|system
  --venv <path>                  For venv/uv: <venv>/bin/python (uv default: .venv)
  --conda-env <name>             For conda: conda run -n <name> python

Optional:
  --repo <path>                  Repo root (default: auto)
  --out-dir <path>               Output dir (default: build_output/pyright)
  --level <error|warning|...>    Default: error
  --report-path <path>           Override report.json path (default: /opt/scimlopsbench/report.json)
  -- <pyright args...>           Extra args passed to Pyright

Outputs (always, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json
EOF
}

mode="system"
repo=""
out_dir=""
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "${SCRIPT_DIR}/.." && pwd)"
repo="${repo:-$REPO_ROOT_DEFAULT}"
out_dir="${out_dir:-build_output/pyright}"
report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

cd "$repo"
mkdir -p "$out_dir"

log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec >"$log_path" 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"
echo "[pyright] report_path=$report_path"
echo "[pyright] mode=$mode"

json_py="$(command -v python3 || true)"
if [[ -z "$json_py" ]]; then
  json_py="$(command -v python || true)"
fi

write_stage_results() {
  local status="$1"; shift
  local exit_code="$1"; shift
  local failure_category="$1"; shift
  local command_str="$1"; shift
  local py_cmd_str="$1"; shift
  local install_attempted="$1"; shift
  local install_cmd="$1"; shift
  local pyright_exit_code="$1"; shift
  local warning="$1"; shift

  local excerpt=""
  excerpt="$("$json_py" - <<PY 2>/dev/null || true
import pathlib
p = pathlib.Path("$log_path")
if not p.exists():
    print("")
    raise SystemExit(0)
txt = p.read_text(encoding="utf-8", errors="replace").splitlines()
print("\\n".join(txt[-220:]))
PY
)"

  STATUS="$status" EXIT_CODE="$exit_code" FAILURE_CATEGORY="$failure_category" COMMAND_STR="$command_str" PY_CMD_STR="$py_cmd_str" \
  INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" PYRIGHT_EXIT_CODE="$pyright_exit_code" WARNING="$warning" EXCERPT="$excerpt" \
  RESULTS_PATH="$results_json" \
  ENSUREPIP_ATTEMPTED="${ensurepip_attempted:-0}" ENSUREPIP_CMD="${ensurepip_cmd:-}" ENSUREPIP_RC="${ensurepip_rc:-}" \
  "$json_py" - <<'PY'
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT, timeout=10
        ).strip()
    except Exception:
        return ""


payload = {
    "status": os.environ.get("STATUS", "failure"),
    "skip_reason": "not_applicable" if os.environ.get("STATUS", "failure") != "skipped" else "unknown",
    "exit_code": int(os.environ.get("EXIT_CODE", "1")),
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
        "python": os.environ.get("PY_CMD_STR", ""),
        "git_commit": git_commit(),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": "Pyright project/targets auto-selected; only reportMissingImports counted.",
        "pyright_ensurepip_attempted": bool(int(os.environ.get("ENSUREPIP_ATTEMPTED", "0") or "0")),
        "pyright_ensurepip_command": os.environ.get("ENSUREPIP_CMD", ""),
        "pyright_ensurepip_rc": int(os.environ.get("ENSUREPIP_RC", "0") or "0"),
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or "0"),
        "warning": os.environ.get("WARNING", ""),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("EXCERPT", ""),
}

Path(os.environ["RESULTS_PATH"]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
}

if [[ -z "$json_py" ]]; then
  echo "[pyright] ERROR: python/python3 not found on PATH; cannot write JSON outputs."
  # Best-effort placeholders.
  printf '%s\n' '{}' >"$out_json"
  printf '%s\n' '{}' >"$analysis_json"
  printf '%s\n' '{"status":"failure","stage":"pyright","failure_category":"deps"}' >"$results_json"
  exit 1
fi

py_cmd=()
warning=""

resolve_report_python() {
  "$json_py" - <<PY 2>/dev/null || true
import json, os, pathlib
rp = pathlib.Path("$report_path")
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
    print(data.get("python_path","") or "")
except Exception:
    print("")
PY
}

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "[pyright] ERROR: --venv required for --mode venv"; write_stage_results failure 1 args_unknown "" "" 0 "" 0 ""; exit 1; }
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "[pyright] ERROR: --conda-env required for --mode conda"; write_stage_results failure 1 args_unknown "" "" 0 "" 0 ""; exit 1; }
      command -v conda >/dev/null 2>&1 || { echo "[pyright] ERROR: conda not found in PATH"; write_stage_results failure 1 deps "" "" 0 "" 0 ""; exit 1; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "[pyright] ERROR: poetry not found in PATH"; write_stage_results failure 1 deps "" "" 0 "" 0 ""; exit 1; }
      py_cmd=(poetry run python)
      ;;
	    system)
	      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
	        py_cmd=("$SCIMLOPSBENCH_PYTHON")
	      else
	        report_py="$(resolve_report_python)"
	        if [[ -n "$report_py" ]]; then
	          py_cmd=("$report_py")
	        else
	          py_cmd=()
	          warning="missing report python_path (no --python/SCIMLOPSBENCH_PYTHON provided)"
	        fi
	      fi
	      ;;
    *)
      echo "[pyright] ERROR: Unknown --mode: $mode"
      usage
      write_stage_results failure 1 args_unknown "" "" 0 "" 0 ""
      exit 2
      ;;
  esac
fi

py_cmd_str="${py_cmd[*]}"
echo "[pyright] python_cmd=$py_cmd_str"

if [[ "${#py_cmd[@]}" -eq 0 ]]; then
  echo "[pyright] ERROR: report missing/invalid and no python override provided."
  printf '%s\n' '{}' >"$out_json"
  printf '%s\n' '{}' >"$analysis_json"
  write_stage_results failure 1 missing_report "pyright (missing report/python_path)" "" 0 "" 0 "$warning"
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] ERROR: Failed to run python via: $py_cmd_str"
  printf '%s\n' '{}' >"$out_json"
  printf '%s\n' '{}' >"$analysis_json"
  write_stage_results failure 1 path_hallucination "" "$py_cmd_str" 0 "" 0 "$warning"
  exit 1
fi

ensurepip_attempted=0
ensurepip_cmd="${py_cmd_str} -m ensurepip --upgrade"
ensurepip_rc=0
if ! "${py_cmd[@]}" -m pip --version >/dev/null 2>&1; then
  ensurepip_attempted=1
  echo "[pyright] pip not available; attempting ensurepip: $ensurepip_cmd"
  set +e
  "${py_cmd[@]}" -m ensurepip --upgrade
  ensurepip_rc=$?
  set -e
  echo "[pyright] ensurepip_rc=$ensurepip_rc"
fi

install_attempted=0
install_cmd="${py_cmd_str} -m pip install -q pyright"
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  echo "[pyright] pyright not importable; attempting install: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pip_rc=$?
  set -e
  if [[ "$pip_rc" -ne 0 ]]; then
    echo "[pyright] ERROR: pyright install failed (rc=$pip_rc)"
    printf '%s\n' '{}' >"$out_json"
    printf '%s\n' '{}' >"$analysis_json"
    fc="deps"
    if rg -n "Temporary failure|Name or service not known|Connection|SSL|403|401" "$log_path" >/dev/null 2>&1; then
      fc="download_failed"
    fi
    write_stage_results failure 1 "$fc" "$install_cmd" "$py_cmd_str" "$install_attempted" "$install_cmd" 0 "$warning"
    exit 1
  fi
fi

project_args=()
targets=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="pyrightconfig.json detected"
elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  decision_reason="[tool.pyright] detected in pyproject.toml"
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
  decision_reason="src/ layout detected"
else
  mapfile -t pkg_dirs < <(find . -name "__init__.py" -not -path "./.venv/*" -not -path "./venv/*" -not -path "./build_output/*" -not -path "./benchmark_assets/*" -not -path "./.git/*" 2>/dev/null | sed 's|^\\./||' | xargs -r -n1 dirname | sort -u)
  if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="package dirs detected via __init__.py"
  fi
fi

if [[ "${#project_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  echo "[pyright] ERROR: Could not determine targets or project config."
  printf '%s\n' '{}' >"$out_json"
  printf '%s\n' '{}' >"$analysis_json"
  write_stage_results failure 1 entrypoint_not_found "pyright (no targets)" "$py_cmd_str" "$install_attempted" "$install_cmd" 0 "$warning"
  exit 1
fi

pyright_cmd=("${py_cmd[@]}" -m pyright)
if [[ "${#targets[@]}" -gt 0 ]]; then
  pyright_cmd+=("${targets[@]}")
else
  # If we have an explicit project file, let it control the analysis scope.
  if [[ "${#project_args[@]}" -eq 0 ]]; then
    pyright_cmd+=(".")
  fi
fi
pyright_cmd+=(--level "$pyright_level" --outputjson)
pyright_cmd+=("${project_args[@]}")
pyright_cmd+=("${pyright_extra_args[@]}")

pyright_cmd_str="$(printf "%q " "${pyright_cmd[@]}")"
echo "[pyright] decision_reason=$decision_reason"
echo "[pyright] command=$pyright_cmd_str"

set +e
"${pyright_cmd[@]}" >"$out_json"
pyright_rc=$?
set -e
echo "[pyright] pyright_rc=$pyright_rc"

if [[ ! -s "$out_json" ]]; then
  printf '%s\n' '{}' >"$out_json"
fi

PYRIGHT_CMD_STR="$pyright_cmd_str" TARGETS="${targets[*]}" DECISION_REASON="$decision_reason" PY_CMD_STR="$py_cmd_str" PYRIGHT_RC="$pyright_rc" \
ENSUREPIP_ATTEMPTED="$ensurepip_attempted" ENSUREPIP_CMD="$ensurepip_cmd" ENSUREPIP_RC="$ensurepip_rc" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" \
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_PATH="$log_path" \
"$json_py" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
from datetime import datetime, timezone
from typing import Iterable

repo_root = pathlib.Path(".").resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_PATH"])

py_cmd_str = os.environ.get("PY_CMD_STR", "")
pyright_rc = int(os.environ.get("PYRIGHT_RC", "0") or "0")
install_attempted = int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")
install_cmd = os.environ.get("INSTALL_CMD", "")
decision_reason = os.environ.get("DECISION_REASON", "")
targets = [t for t in os.environ.get("TARGETS", "").split() if t]

def git_commit() -> str:
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], text=True, stderr=subprocess.STDOUT, timeout=10).strip()
    except Exception:
        return ""

def tail_lines(path: pathlib.Path, max_lines: int = 220, max_bytes: int = 128 * 1024) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        txt = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join(txt[-max_lines:])
    except Exception:
        return ""

def safe_load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

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
        "outputs",
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

analysis_payload = {
    "missing_packages": missing_packages,
    "missing_imports_diagnostics": missing_diags,
    "pyright": data,
    "meta": {
        "python_cmd": py_cmd_str,
        "pyright_exit_code": pyright_rc,
        "targets": targets,
        "files_scanned": files_scanned,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision_reason": decision_reason,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "success",
    "skip_reason": "not_applicable",
    "exit_code": 0,
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
	        "python": py_cmd_str,
	        "git_commit": git_commit(),
	        "env_vars": {},
	        "decision_reason": decision_reason,
	        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	        "pyright_exit_code": pyright_rc,
	        "pyright_ensurepip_attempted": bool(int(os.environ.get("ENSUREPIP_ATTEMPTED","0") or "0")),
	        "pyright_ensurepip_command": os.environ.get("ENSUREPIP_CMD",""),
	        "pyright_ensurepip_rc": int(os.environ.get("ENSUREPIP_RC","0") or "0"),
	        "pyright_install_attempted": bool(install_attempted),
	        "pyright_install_command": install_cmd,
	        "targets": targets,
	    },
    "metrics": analysis_payload["metrics"],
    "missing_packages": missing_packages,
    "failure_category": "unknown",
    "error_excerpt": tail_lines(log_path),
}
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# Ensure results.json exists even if analysis step failed.
if [[ ! -s "$results_json" ]]; then
  echo "[pyright] ERROR: analysis did not produce results.json"
  printf '%s\n' '{}' >"$analysis_json"
  write_stage_results failure 1 unknown "$pyright_cmd_str" "$py_cmd_str" "$install_attempted" "$install_cmd" "$pyright_rc" "$warning"
  exit 1
fi

echo "[pyright] done"
exit 0
