#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule=reportMissingImports).

Outputs (always):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json   # raw Pyright JSON output (if available)
  build_output/pyright/analysis.json         # parsed diagnostics + metrics
  build_output/pyright/results.json          # stage result envelope + metrics

Environment selection:
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report (preferred), else python from PATH

Options:
  --repo <path>                  Repository root (default: repo root inferred from this script)
  --level <error|warning|...>    Pyright diagnostics level (default: error)
  --report-path <path>           Override agent report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes kvpress)
EOF
}

mode="system"
repo=""
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

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_default="$(cd -- "${script_dir}/.." && pwd)"
repo="${repo:-$repo_default}"

out_dir="$repo/build_output/pyright"
log_path="$out_dir/log.txt"
pyright_out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

mkdir -p "$out_dir"
: >"$log_path"

status="failure"
skip_reason="unknown"
exit_code=1
failure_category="unknown"
decision_reason=""
install_attempted=0
install_cmd=""
pyright_rc=""
py_cmd=()
py_cmd_str=""
pyright_cmd_str=""

log() { printf '%s\n' "$*" | tee -a "$log_path" >/dev/null; }

resolve_report_path() {
  if [[ -n "$report_path" ]]; then
    printf '%s' "$report_path"
  elif [[ -n "${SCIMLOPSBENCH_REPORT:-}" ]]; then
    printf '%s' "$SCIMLOPSBENCH_REPORT"
  else
    printf '%s' "/opt/scimlopsbench/report.json"
  fi
}

report_p="$(resolve_report_path)"

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        log "ERROR: --venv is required for --mode venv"
        failure_category="args_unknown"
        goto_finalize=1
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
        log "ERROR: --conda-env is required for --mode conda"
        failure_category="args_unknown"
        goto_finalize=1
      else
        if ! command -v conda >/dev/null 2>&1; then
          log "ERROR: conda not found in PATH"
          failure_category="deps"
          goto_finalize=1
        else
          py_cmd=(conda run -n "$conda_env" python)
        fi
      fi
      ;;
    poetry)
      if ! command -v poetry >/dev/null 2>&1; then
        log "ERROR: poetry not found in PATH"
        failure_category="deps"
        goto_finalize=1
      else
        py_cmd=(poetry run python)
      fi
      ;;
    system)
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("$SCIMLOPSBENCH_PYTHON")
        decision_reason="Used SCIMLOPSBENCH_PYTHON env var"
      elif [[ -f "$report_p" ]]; then
        python_from_report="$(python3 - <<PY 2>/dev/null || true
import json, sys
p = "$report_p"
try:
  d = json.load(open(p, "r", encoding="utf-8"))
  print(d.get("python_path","") or "")
except Exception:
  print("")
PY
)"
        if [[ -n "$python_from_report" ]]; then
          py_cmd=("$python_from_report")
          decision_reason="Used python_path from agent report: $report_p"
        else
          log "ERROR: report exists but python_path missing/invalid: $report_p"
          failure_category="missing_report"
          goto_finalize=1
        fi
      else
        log "ERROR: missing agent report (provide --python or --report-path): $report_p"
        failure_category="missing_report"
        goto_finalize=1
      fi
      ;;
    *)
      log "ERROR: Unknown --mode: $mode"
      failure_category="args_unknown"
      goto_finalize=1
      ;;
  esac
fi

py_cmd_str="${py_cmd[*]:-}"

if [[ "${goto_finalize:-0}" -eq 1 ]]; then
  : >"$pyright_out_json"
  : >"$analysis_json"
else
  cd "$repo"
  log "Repo: $repo"
  log "Python command: $py_cmd_str"

  if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>>"$log_path"; then
    log "ERROR: Failed to run python via: $py_cmd_str"
    failure_category="deps"
    : >"$pyright_out_json"
    : >"$analysis_json"
    goto_finalize=1
  fi
fi

pyright_targets=()
pyright_args=()

if [[ "${goto_finalize:-0}" -ne 1 ]]; then
  if [[ -f "pyrightconfig.json" ]]; then
    pyright_args+=(--project pyrightconfig.json)
    decision_reason="${decision_reason:+$decision_reason; }Detected pyrightconfig.json"
  else
    has_tool_pyright=0
    if [[ -f "pyproject.toml" ]]; then
      if [[ "$(python3 - <<'PY' 2>/dev/null || true
import re, pathlib
txt = pathlib.Path("pyproject.toml").read_text(encoding="utf-8", errors="ignore")
print("1" if re.search(r"^\\[tool\\.pyright\\]\\s*$", txt, flags=re.MULTILINE) else "0")
PY
)" == "1" ]]; then
        has_tool_pyright=1
      fi
    fi
    if [[ "$has_tool_pyright" -eq 1 ]]; then
      pyright_args+=(--project pyproject.toml)
      decision_reason="${decision_reason:+$decision_reason; }Detected [tool.pyright] in pyproject.toml"
    elif [[ -d "src" ]]; then
      pyright_targets+=(src)
      [[ -d "tests" ]] && pyright_targets+=(tests)
      decision_reason="${decision_reason:+$decision_reason; }Detected src/ layout"
    else
      for d in */; do
        case "$d" in
          .git/|.venv/|venv/|build/|dist/|node_modules/|benchmark_scripts/|benchmark_assets/|build_output/)
            continue
            ;;
        esac
        if find "$d" -type f -name "*.py" -print -quit 2>/dev/null | grep -q .; then
          pyright_targets+=("${d%/}")
        fi
      done
      if [[ ${#pyright_targets[@]} -gt 0 ]]; then
        decision_reason="${decision_reason:+$decision_reason; }Detected python directories: ${pyright_targets[*]}"
      fi
    fi
  fi

  if [[ ${#pyright_args[@]} -eq 0 && ${#pyright_targets[@]} -eq 0 ]]; then
    log "ERROR: Could not determine Pyright targets (no pyrightconfig/pyproject, no src/, no Python dirs)."
    failure_category="entrypoint_not_found"
    : >"$pyright_out_json"
    : >"$analysis_json"
    goto_finalize=1
  fi
fi

if [[ "${goto_finalize:-0}" -ne 1 ]]; then
  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>>"$log_path"; then
    install_attempted=1
    install_cmd="${py_cmd_str} -m pip install -q pyright"
    log "Pyright missing; attempting install: $install_cmd"
    if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_path" 2>&1; then
      log "ERROR: Failed to install pyright."
      if command -v rg >/dev/null 2>&1; then
        if rg -n "No matching distribution|Could not find a version|Temporary failure in name resolution|Connection (refused|timed out)|SSLError|ReadTimeout|ProxyError" -S "$log_path" >/dev/null 2>&1; then
          failure_category="download_failed"
        else
          failure_category="deps"
        fi
      else
        if grep -E "No matching distribution|Could not find a version|Temporary failure in name resolution|Connection (refused|timed out)|SSLError|ReadTimeout|ProxyError" "$log_path" >/dev/null 2>&1; then
          failure_category="download_failed"
        else
          failure_category="deps"
        fi
      fi
      : >"$pyright_out_json"
      : >"$analysis_json"
      goto_finalize=1
    fi
  fi
fi

if [[ "${goto_finalize:-0}" -ne 1 ]]; then
  pyright_cmd=("${py_cmd[@]}" -m pyright)
  if [[ ${#pyright_targets[@]} -gt 0 ]]; then
    pyright_cmd+=("${pyright_targets[@]}")
  fi
  pyright_cmd+=(--level "$pyright_level" --outputjson)
  pyright_cmd+=("${pyright_args[@]}")
  pyright_cmd+=("${pyright_extra_args[@]}")
  pyright_cmd_str="${pyright_cmd[*]}"

  log "Running: $pyright_cmd_str"
  pyright_rc=0
  "${pyright_cmd[@]}" >"$pyright_out_json" 2>>"$log_path"
  pyright_rc="$?"
  log "Pyright exit code: $pyright_rc (ignored for stage status if JSON was produced)"

  if [[ ! -s "$pyright_out_json" ]]; then
    log "ERROR: pyright_output.json not produced."
    failure_category="runtime"
    : >"$analysis_json"
    goto_finalize=1
  fi
fi

python3 - <<PY 2>>"$log_path" || true
import ast
import json
import os
import pathlib
import re
from typing import Iterable

repo = pathlib.Path("$repo").resolve()
out_dir = pathlib.Path("$out_dir").resolve()
pyright_out = pathlib.Path("$pyright_out_json")
analysis_json = pathlib.Path("$analysis_json")
results_json = pathlib.Path("$results_json")
log_path = pathlib.Path("$log_path")

def tail_lines(path: pathlib.Path, max_lines: int = 220) -> str:
  try:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]).strip()
  except Exception:
    return ""

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
    "benchmark_assets",
    "benchmark_scripts",
    "build_output",
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
      if getattr(node, "level", 0) == 0 and getattr(node, "module", None):
        pkgs.add(str(node.module).split(".")[0])
  return pkgs

pyright_data = {}
pyright_parse_ok = False
if pyright_out.exists() and pyright_out.stat().st_size > 0:
  try:
    pyright_data = json.loads(pyright_out.read_text(encoding="utf-8"))
    pyright_parse_ok = True
  except Exception:
    pyright_data = {}

diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import \"([^.\"\\s]+)')
missing_packages = sorted(
  {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(repo):
  files_scanned += 1
  all_imported_packages |= collect_imported_packages(py_file)

metrics = {
  "missing_packages_count": len(missing_packages),
  "total_imported_packages_count": len(all_imported_packages),
  "missing_package_ratio": f"{len(missing_packages)}/{len(all_imported_packages)}",
}

analysis_payload = {
  "missing_packages": missing_packages,
  "missing_diagnostics": missing_diags,
  "pyright": pyright_data if pyright_parse_ok else {},
  "meta": {
    "repo": str(repo),
    "files_scanned": files_scanned,
    "pyright_parse_ok": pyright_parse_ok,
  },
  "metrics": metrics,
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# results.json is written by bash finalize step; keep this file valid even if finalize fails.
if not results_json.exists():
  results_json.write_text(json.dumps({"stage":"pyright","status":"failure","exit_code":1,"failure_category":"unknown","error_excerpt":tail_lines(log_path)}, indent=2) + "\n", encoding="utf-8")
PY

finalize_status="failure"
finalize_exit_code=1
finalize_failure_category="$failure_category"

if [[ "${goto_finalize:-0}" -ne 1 ]]; then
  finalize_status="success"
  finalize_exit_code=0
  finalize_failure_category="not_applicable"
fi

python3 - <<PY >"$results_json" 2>>"$log_path"
import json, os, pathlib, subprocess

out_dir = pathlib.Path("$out_dir").resolve()
analysis_json = out_dir / "analysis.json"
log_path = out_dir / "log.txt"
report_path = pathlib.Path("$report_p")

def tail_lines(path: pathlib.Path, max_lines: int = 220) -> str:
  try:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]).strip()
  except Exception:
    return ""

metrics = {}
try:
  metrics = json.loads(analysis_json.read_text(encoding="utf-8")).get("metrics", {})
except Exception:
  metrics = {}

git_commit = ""
try:
  git_commit = subprocess.check_output(["git","rev-parse","HEAD"], cwd=str(pathlib.Path("$repo")), text=True).strip()
except Exception:
  git_commit = ""

payload = {
  "status": "$finalize_status",
  "skip_reason": "not_applicable",
  "exit_code": int("$finalize_exit_code"),
  "stage": "pyright",
  "task": "check",
  "command": "$pyright_cmd_str",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "$py_cmd_str",
    "git_commit": git_commit,
    "env_vars": {k: os.environ.get(k,"") for k in [
      "SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","PYTHONPATH"
    ] if os.environ.get(k)},
    "decision_reason": "$decision_reason",
    "pyright_exit_code": "$pyright_rc",
    "pyright_install_attempted": bool(int("$install_attempted")),
    "pyright_install_command": "$install_cmd",
    "report_path": str(report_path),
  },
  "failure_category": "$finalize_failure_category",
  "error_excerpt": tail_lines(log_path),
  "metrics": metrics,
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

if [[ "$finalize_status" == "success" ]]; then
  exit 0
fi
exit 1
