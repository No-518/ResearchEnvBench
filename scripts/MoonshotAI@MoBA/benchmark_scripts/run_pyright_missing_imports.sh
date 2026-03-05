#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (fixed):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection (pick ONE; if none provided, use python_path from report.json):
  --python <path>                Explicit python executable (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --repo <path>                  Repo root (default: inferred)
  --level <error|warning|...>    Default: error
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  -- <pyright args...>           Extra args passed to Pyright
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$repo_root"
mode=""
level="error"
python_bin=""
venv_dir=""
conda_env=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    --level) level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pyright_extra_args=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

out_dir="$repo/build_output/pyright"
mkdir -p "$out_dir"
log_txt="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

install_attempted=0
install_cmd=""
pyright_cmd=""
decision_reason=""
failure_category="unknown"

json_py="$(command -v python 2>/dev/null || true)"
if [[ -z "$json_py" ]]; then
  json_py="$(command -v python3 2>/dev/null || true)"
fi

json_escape() {
  # Reads stdin, writes JSON string literal (including quotes).
  if [[ -n "$json_py" ]]; then
    "$json_py" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
  else
    # Very small fallback; not fully general, but avoids crashing.
    printf '%s' "\"$(sed 's/\\/\\\\/g; s/\"/\\\\\"/g' | tr -d '\r')\""
  fi
}

ensure_artifacts() {
  [[ -f "$out_json" ]] || echo '{}' >"$out_json"
  [[ -f "$analysis_json" ]] || echo '{}' >"$analysis_json"
  [[ -f "$results_json" ]] || true
}

write_results_py() {
  local status="$1"
  local exit_code="$2"
  local skip_reason="$3"

  ensure_artifacts

  local python_for_meta
  python_for_meta="${python_bin:-}"
  if [[ -z "$python_for_meta" ]]; then
    python_for_meta="$(command -v python 2>/dev/null || true)"
  fi

  local git_commit=""
  git_commit="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"

  local error_excerpt=""
  error_excerpt="$(tail -n 220 "$log_txt" 2>/dev/null || true)"

  cat >"$results_json" <<JSON
{
  "status": "$(printf '%s' "$status")",
  "skip_reason": "$(printf '%s' "$skip_reason")",
  "exit_code": $exit_code,
  "stage": "pyright",
  "task": "check",
  "command": $(printf '%s' "$pyright_cmd" | json_escape),
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": $(printf '%s' "$python_for_meta" | json_escape),
    "git_commit": $(printf '%s' "$git_commit" | json_escape),
    "env_vars": {
      "mode": $(printf '%s' "${mode:-}" | json_escape),
      "report_path": $(printf '%s' "$report_path" | json_escape)
    },
    "decision_reason": $(printf '%s' "$decision_reason" | json_escape),
    "pyright_install_attempted": $install_attempted,
    "pyright_install_command": $(printf '%s' "$install_cmd" | json_escape)
  },
  "failure_category": "$(printf '%s' "$failure_category")",
  "error_excerpt": $(printf '%s' "$error_excerpt" | json_escape)
}
JSON
}

{
  echo "[pyright] repo=$repo"
  echo "[pyright] report_path=$report_path"
} >"$log_txt"

if [[ -z "$repo" ]]; then
  failure_category="args_unknown"
  decision_reason="--repo resolved to empty."
  write_results_py "failure" 1 "unknown"
  exit 1
fi

cd "$repo" || {
  failure_category="entrypoint_not_found"
  decision_reason="Failed to cd into repo: $repo"
  write_results_py "failure" 1 "unknown"
  exit 1
}

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  if [[ -z "$mode" ]]; then
    # Default: read python_path from report.json.
    if [[ ! -f "$report_path" ]]; then
      failure_category="missing_report"
      decision_reason="No --python/--mode provided, and report.json not found at $report_path."
      ensure_artifacts
      write_results_py "failure" 1 "unknown"
      exit 1
    fi
    python_bin="$(python - <<PY 2>>"$log_txt" || true
import json,sys
from pathlib import Path
p = Path(${report_path@Q})
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("python_path",""))
except Exception:
    print("")
PY
)"
    if [[ -z "$python_bin" ]]; then
      failure_category="missing_report"
      decision_reason="report.json missing/invalid or python_path not set."
      ensure_artifacts
      write_results_py "failure" 1 "unknown"
      exit 1
    fi
    py_cmd=("$python_bin")
    mode="report"
  else
    case "$mode" in
      venv)
        if [[ -z "$venv_dir" ]]; then
          failure_category="args_unknown"
          decision_reason="--venv is required for --mode venv"
          write_results_py "failure" 1 "unknown"
          exit 1
        fi
        py_cmd=("$venv_dir/bin/python")
        ;;
      uv)
        venv_dir="${venv_dir:-.venv}"
        py_cmd=("$venv_dir/bin/python")
        ;;
      conda)
        if [[ -z "$conda_env" ]]; then
          failure_category="args_unknown"
          decision_reason="--conda-env is required for --mode conda"
          write_results_py "failure" 1 "unknown"
          exit 1
        fi
        if ! command -v conda >/dev/null 2>&1; then
          failure_category="deps"
          decision_reason="conda not found in PATH"
          write_results_py "failure" 1 "unknown"
          exit 1
        fi
        py_cmd=(conda run -n "$conda_env" python)
        ;;
      poetry)
        if ! command -v poetry >/dev/null 2>&1; then
          failure_category="deps"
          decision_reason="poetry not found in PATH"
          write_results_py "failure" 1 "unknown"
          exit 1
        fi
        py_cmd=(poetry run python)
        ;;
      system)
        py_cmd=(python)
        ;;
      *)
        failure_category="args_unknown"
        decision_reason="Unknown --mode: $mode"
        write_results_py "failure" 1 "unknown"
        exit 1
        ;;
    esac
  fi
fi

{
  echo "[pyright] mode=$mode"
  echo "[pyright] python_cmd=${py_cmd[*]}"
} >>"$log_txt"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_txt" 2>&1; then
  failure_category="path_hallucination"
  decision_reason="Failed to run python via: ${py_cmd[*]}"
  ensure_artifacts
  write_results_py "failure" 1 "unknown"
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_txt" 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] installing: $install_cmd" >>"$log_txt"
  if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_txt" 2>&1; then
    # Best-effort categorization.
    if rg -n "Temporary failure|Name or service not known|Connection|timed out|SSL" "$log_txt" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    decision_reason="Failed to install pyright into selected environment."
    ensure_artifacts
    write_results_py "failure" 1 "unknown"
    exit 1
  fi
fi

# Decide pyright targets/project.
project_args=()
targets=()
if [[ -f pyrightconfig.json ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="Using pyrightconfig.json"
elif [[ -f pyproject.toml ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  decision_reason="Using [tool.pyright] from pyproject.toml"
elif [[ -d src ]]; then
  targets=(src)
  [[ -d tests ]] && targets+=(tests)
  decision_reason="Using src/ layout targets"
else
  mapfile -t pkg_dirs < <(find . -maxdepth 3 -type f -name "__init__.py" -print 2>/dev/null | sed 's#/__init__\\.py$##' | sed 's#^\\./##' | sort -u)
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="Detected package dirs via __init__.py"
  else
    failure_category="entrypoint_not_found"
    decision_reason="No pyright config, no src/, and no package dirs with __init__.py found."
    ensure_artifacts
    write_results_py "failure" 1 "unknown"
    exit 1
  fi
fi

# Run Pyright; non-zero exit is expected when diagnostics are present.
pyright_cmd="${py_cmd[*]} -m pyright ${project_args[*]} --level $level --outputjson ${pyright_extra_args[*]} ${targets[*]}"
{
  echo "[pyright] command: $pyright_cmd"
  echo "[pyright] targets: ${targets[*]}"
  echo "[pyright] project_args: ${project_args[*]}"
} >>"$log_txt"

"${py_cmd[@]}" -m pyright "${project_args[@]}" --level "$level" --outputjson "${pyright_extra_args[@]}" "${targets[@]}" >"$out_json" 2>>"$log_txt" || true

# Parse output and compute missing import metrics.
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" REPO_ROOT="$repo" \
PY_CMD="${py_cmd[*]}" MODE="$mode" INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" \
DECISION_REASON="$decision_reason" PYRIGHT_CMD="$pyright_cmd" \
"${py_cmd[@]}" - <<'PY' >>"$log_txt" 2>&1
import ast
import json
import os
import pathlib
import re
import subprocess
from typing import Iterable

repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=5).strip()
    except Exception:
        return ""

def _read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

data = _read_json(out_json)
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
        "benchmark_assets",
        "build_output",
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
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "pyright_command": os.environ.get("PYRIGHT_CMD", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "git_commit": _git_commit(),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Stage results.json: keep the full schema and attach metrics.
results_payload = {
    "status": "success",
    "skip_reason": "unknown",
    "exit_code": 0,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "metrics": analysis_payload["metrics"],
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": _git_commit(),
        "env_vars": {"mode": os.environ.get("MODE", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": analysis_payload["meta"]["pyright_install_attempted"],
        "pyright_install_command": analysis_payload["meta"]["pyright_install_command"],
    },
    "failure_category": "unknown",
    "error_excerpt": "",
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"missing_imports={missing_packages_count} total_imports={total_imported_packages_count}")
PY

# If python parsing wrote results.json, we treat stage as success.
if [[ -f "$results_json" ]]; then
  exit 0
fi

failure_category="invalid_json"
decision_reason="Failed to generate results.json from pyright output."
write_results_py "failure" 1 "unknown"
exit 1
