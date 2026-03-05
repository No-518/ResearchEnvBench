#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright in an already-configured environment and report only missing-import diagnostics (reportMissingImports).

Required:
  --repo <path>                  Path to the repository/project to analyze

Outputs (always written under repo root):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Python selection:
  --python <path>                Explicit python executable (highest)
  --mode venv  --venv <path>     Use <venv>/bin/python
  --mode uv   [--venv <path>]    Use <venv>/bin/python (default: .venv)
  --mode conda --conda-env <n>   Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

If none of the above are provided, the script uses python_path from:
  ${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}

Other:
  --level <error|warning|...>    Default: error
  --out-dir <path>               Must be build_output (default: build_output)
  -- <pyright args...>           Extra args passed to Pyright
EOF
}

repo_root_default="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$repo_root_default"
repo_arg=""
out_dir="build_output"

# Pre-scan argv for --repo/--out-dir so we can create outputs even on arg errors.
argv=("$@")
idx=0
while [[ $idx -lt ${#argv[@]} ]]; do
  case "${argv[$idx]}" in
    --repo)
      repo_arg="${argv[$((idx+1))]:-}"
      idx=$((idx+2))
      ;;
    --out-dir)
      out_dir="${argv[$((idx+1))]:-}"
      idx=$((idx+2))
      ;;
    *)
      idx=$((idx+1))
      ;;
  esac
done

if [[ -n "$repo_arg" && -d "$repo_arg" ]]; then
  repo_root="$(cd "$repo_arg" && pwd)"
fi

if [[ -z "$out_dir" ]]; then
  out_dir="build_output"
fi

stage_dir="$repo_root/build_output/pyright"
log_path="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

mkdir -p "$stage_dir"
mkdir -p "$repo_root/benchmark_assets/cache/pycache" "$repo_root/benchmark_assets/cache/xdg" "$repo_root/benchmark_assets/cache/torch" "$repo_root/benchmark_assets/cache/hf"
export PYTHONPYCACHEPREFIX="$repo_root/benchmark_assets/cache/pycache"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export HF_HOME="$repo_root/benchmark_assets/cache/hf"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf"

: >"$log_path"
echo "{}" >"$out_json"
echo "{}" >"$analysis_json"
echo "{}" >"$results_json"

note() { echo "[pyright] $*" >>"$log_path"; }

write_failure() {
  local failure_category="$1"; shift
  local decision_reason="$1"; shift || true
  local cmd_str="${1:-}"
  PY_CMD_STR="${PY_CMD_STR:-}" INSTALL_ATTEMPTED="${INSTALL_ATTEMPTED:-0}" INSTALL_CMD="${INSTALL_CMD:-}" \
  DECISION_REASON="$decision_reason" FAILURE_CATEGORY="$failure_category" COMMAND_STR="$cmd_str" \
  GIT_COMMIT="${GIT_COMMIT:-}" REPORT_PATH="${REPORT_PATH:-}" LOG_PATH="$log_path" RESULTS_JSON="$results_json" \
  python - <<'PY'
import json, os, pathlib, time

def tail(path: str, n: int = 240) -> str:
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
    "meta": {
        "python": os.environ.get("PY_CMD_STR", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("REPORT_PATH", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(os.environ.get("LOG_PATH", "")),
}
pathlib.Path(os.environ["RESULTS_JSON"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

mode=""
python_bin=""
venv_dir=""
conda_env=""
pyright_level="error"
repo=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      repo="${2:-}"; shift 2 ;;
    --out-dir)
      out_dir="${2:-}"; shift 2 ;;
    --mode)
      mode="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --venv)
      venv_dir="${2:-}"; shift 2 ;;
    --conda-env)
      conda_env="${2:-}"; shift 2 ;;
    --level)
      pyright_level="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift
      pyright_extra_args=("$@")
      break
      ;;
    *)
      note "Unknown argument: $1"
      write_failure "args_unknown" "Unknown CLI argument: $1" "$0 ${argv[*]}"
      exit 1 ;;
  esac
done

if [[ -z "$repo" ]]; then
  note "Missing required --repo"
  write_failure "args_unknown" "--repo is required" "$0 ${argv[*]}"
  exit 1
fi

if [[ -n "${out_dir:-}" && "$out_dir" != "build_output" ]]; then
  note "Unsupported --out-dir (must be build_output): $out_dir"
  write_failure "args_unknown" "--out-dir must be build_output to satisfy benchmark directory contract" "$0 ${argv[*]}"
  exit 1
fi

if [[ ! -d "$repo" ]]; then
  note "Repo dir not found: $repo"
  write_failure "entrypoint_not_found" "Repo path does not exist: $repo" "$0 ${argv[*]}"
  exit 1
fi

repo_root="$(cd "$repo" && pwd)"
stage_dir="$repo_root/build_output/pyright"
log_path="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"
mkdir -p "$stage_dir"

mkdir -p "$repo_root/benchmark_assets/cache/pycache" "$repo_root/benchmark_assets/cache/xdg" "$repo_root/benchmark_assets/cache/torch" "$repo_root/benchmark_assets/cache/hf"
export PYTHONPYCACHEPREFIX="$repo_root/benchmark_assets/cache/pycache"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export HF_HOME="$repo_root/benchmark_assets/cache/hf"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf"

: >"$log_path"
echo "{}" >"$out_json"
echo "{}" >"$analysis_json"
echo "{}" >"$results_json"

GIT_COMMIT="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
REPORT_PATH="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

install_attempted=0
install_cmd=""
PY_CMD_STR=""

resolve_report_python() {
  if [[ ! -f "$REPORT_PATH" ]]; then
    echo ""
    return 0
  fi
  python - <<PY 2>>"$log_path" || true
import json
try:
  data=json.load(open("${REPORT_PATH}","r",encoding="utf-8"))
  print(data.get("python_path","") or "")
except Exception:
  print("")
PY
}

py_cmd=()
decision_reason=""

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  decision_reason="Using --python override."
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("$SCIMLOPSBENCH_PYTHON")
  decision_reason="Using SCIMLOPSBENCH_PYTHON override."
elif [[ -n "$mode" ]]; then
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        write_failure "args_unknown" "--mode venv requires --venv" "$0 ${argv[*]}"
        exit 1
      fi
      py_cmd=("$venv_dir/bin/python")
      decision_reason="Using venv python at $venv_dir/bin/python"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      decision_reason="Using uv-style venv python at $venv_dir/bin/python"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        write_failure "args_unknown" "--mode conda requires --conda-env" "$0 ${argv[*]}"
        exit 1
      fi
      command -v conda >/dev/null 2>&1 || { write_failure "deps" "conda not found in PATH" "$0 ${argv[*]}"; exit 1; }
      py_cmd=(conda run -n "$conda_env" python)
      decision_reason="Using conda env: $conda_env"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { write_failure "deps" "poetry not found in PATH" "$0 ${argv[*]}"; exit 1; }
      py_cmd=(poetry run python)
      decision_reason="Using poetry run python"
      ;;
    system)
      py_cmd=(python)
      decision_reason="Using system python from PATH"
      ;;
    *)
      write_failure "args_unknown" "Unknown --mode: $mode" "$0 ${argv[*]}"
      exit 1
      ;;
  esac
else
  report_python="$(resolve_report_python)"
  if [[ -z "$report_python" ]]; then
    write_failure "missing_report" "No --python/SCIMLOPSBENCH_PYTHON/--mode provided and report.json missing/invalid." "$0 ${argv[*]}"
    exit 1
  fi
  py_cmd=("$report_python")
  decision_reason="Using python_path from agent report.json"
fi

PY_CMD_STR="${py_cmd[*]}"
note "repo_root=$repo_root"
note "report_path=$REPORT_PATH"
note "python_cmd=$PY_CMD_STR"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_path" 2>&1; then
  write_failure "deps" "Resolved python is not runnable: $PY_CMD_STR" "$0 ${argv[*]}"
  exit 1
fi

# Ensure pyright exists; if not, install into selected environment.
if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_path" 2>&1; then
  install_attempted=1
  install_cmd="${PY_CMD_STR} -m pip install -q pyright"
  note "Installing pyright: $install_cmd"
  if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_path" 2>&1; then
    if grep -nE "(Temporary failure in name resolution|ConnectionError|SSL|No matching distribution found|Could not fetch URL)" "$log_path" >/dev/null 2>&1; then
      FAILURE_CATEGORY="download_failed"
    else
      FAILURE_CATEGORY="deps"
    fi
    INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" PY_CMD_STR="$PY_CMD_STR" GIT_COMMIT="$GIT_COMMIT" REPORT_PATH="$REPORT_PATH"
    write_failure "$FAILURE_CATEGORY" "Pyright not available and installation failed." "$install_cmd"
    exit 1
  fi
fi

# Determine what to analyze.
project_args=()
targets=()
if [[ -f "$repo_root/pyrightconfig.json" ]]; then
  project_args=(--project "$repo_root/pyrightconfig.json")
  decision_reason="${decision_reason} | Detected pyrightconfig.json"
elif [[ -f "$repo_root/pyproject.toml" ]] && grep -n "\\[tool\\.pyright\\]" "$repo_root/pyproject.toml" >/dev/null 2>&1; then
  project_args=(--project "$repo_root/pyproject.toml")
  decision_reason="${decision_reason} | Detected [tool.pyright] in pyproject.toml"
elif [[ -d "$repo_root/src" ]]; then
  targets=("src")
  if [[ -d "$repo_root/tests" ]]; then
    targets+=("tests")
  fi
  decision_reason="${decision_reason} | Detected src/ layout"
else
  mapfile -t targets < <(REPO_ROOT="$repo_root" python - <<'PY'
import os
from pathlib import Path

repo = Path(os.environ["REPO_ROOT"]).resolve()
exclude = {
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

pkg_dirs = []
for p in repo.rglob("__init__.py"):
    if any(part in exclude for part in p.parts):
        continue
    pkg_dirs.append(p.parent)

pkg_dirs = sorted({p for p in pkg_dirs})
roots = []
for p in pkg_dirs:
    if not any(str(p).startswith(str(r) + os.sep) for r in roots):
        roots.append(p)

for p in roots:
    print(str(p.relative_to(repo)))
PY
)
  if [[ ${#targets[@]} -eq 0 ]]; then
    note "No Python package directories (with __init__.py) detected."
    INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" PY_CMD_STR="$PY_CMD_STR" GIT_COMMIT="$GIT_COMMIT" REPORT_PATH="$REPORT_PATH"
    write_failure "entrypoint_not_found" "No pyrightconfig/pyproject, no src/, and no package dirs detected." ""
    exit 1
  fi
  decision_reason="${decision_reason} | Detected package dirs via __init__.py"
fi

note "project_args=${project_args[*]:-<none>}"
note "targets=${targets[*]:-<none>}"

# Run pyright. Non-zero exit from pyright should not fail this stage.
command_str="${PY_CMD_STR} -m pyright --outputjson --level ${pyright_level} ${project_args[*]:-} ${pyright_extra_args[*]:-} ${targets[*]:-}"
note "Running: $command_str"

set +e
"${py_cmd[@]}" -m pyright --outputjson --level "$pyright_level" "${project_args[@]}" "${pyright_extra_args[@]}" "${targets[@]}" >"$out_json" 2>>"$log_path"
pyright_rc=$?
set -e
note "pyright_exit_code=$pyright_rc (ignored for stage status)"

PY_CMD_STR="$PY_CMD_STR" INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" DECISION_REASON="$decision_reason" \
COMMAND_STR="$command_str" GIT_COMMIT="$GIT_COMMIT" REPORT_PATH="$REPORT_PATH" PYRIGHT_RC="$pyright_rc" \
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_PATH="$log_path" \
TARGETS="${targets[*]:-}" PROJECT_ARGS="${project_args[*]:-}" REPO_ROOT="$repo_root" \
"${py_cmd[@]}" - <<'PY' 2>>"$log_path"
import ast
import json
import os
import pathlib
import re
import time
from typing import Iterable

repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_PATH"])

def tail(path: pathlib.Path, n: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

pyright_data: dict = {}
parse_err = ""
try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    parse_err = f"{type(e).__name__}: {e}"
    pyright_data = {"__parse_error__": parse_err}

diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import \"([^\"]+)\"')
missing_packages = sorted(
    {pattern.search(d.get("message", "")).group(1).split(".")[0] for d in missing_diags if pattern.search(d.get("message", ""))}
)

targets_str = os.environ.get("TARGETS", "").strip()
targets = [t for t in targets_str.split() if t]
if not targets:
    targets = ["."]

def iter_py_files(roots: list[pathlib.Path]) -> Iterable[pathlib.Path]:
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
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
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

roots = [(repo_root / t).resolve() for t in targets]
all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": pyright_data,
    "meta": {
        "python_cmd": os.environ.get("PY_CMD_STR", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_RC", "0") or 0),
        "targets": targets,
        "project_args": os.environ.get("PROJECT_ARGS", ""),
        "files_scanned": files_scanned,
        "pyright_output_parse_error": parse_err,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

status = "success"
exit_code = 0
failure_category = "none"
if parse_err:
    status = "failure"
    exit_code = 1
    failure_category = "invalid_json"

results_payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
    "metrics": analysis_payload["metrics"],
    "meta": {
        "python": os.environ.get("PY_CMD_STR", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("REPORT_PATH", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "targets": targets,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "failure_category": failure_category,
    "error_excerpt": tail(log_path) if status != "success" else "",
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

final_status="$(python - <<'PY' 2>/dev/null || true
import json, pathlib
try:
  d=json.loads(pathlib.Path("build_output/pyright/results.json").read_text(encoding="utf-8"))
  print(d.get("status","failure"))
  print(int(d.get("exit_code",1)))
except Exception:
  print("failure")
  print("1")
PY
)"
status_line="$(echo "$final_status" | head -n 1)"
exit_line="$(echo "$final_status" | tail -n 1)"

if [[ "$status_line" == "success" && "$exit_line" == "0" ]]; then
  exit 0
fi
exit 1
