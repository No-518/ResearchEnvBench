#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (always, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to repository root

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --level <error|warning|...>    Default: error
  --install-pyright              Ignored (pyright is always auto-installed if missing)
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes pkg)
EOF
}

mode="system"
repo=""
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
install_pyright=0
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --install-pyright) install_pyright=1; shift ;;
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

repo="$(cd "$repo" && pwd)"
cd "$repo" || exit 1

out_dir="$repo/build_output/pyright"
mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec > >(tee "$log_file") 2>&1

py_cmd=()
py_cmd_human=""
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  py_cmd_human="$python_bin"
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then echo "--venv is required for --mode venv" >&2; exit 2; fi
      py_cmd=("$venv_dir/bin/python"); py_cmd_human="${py_cmd[*]}" ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python"); py_cmd_human="${py_cmd[*]}" ;;
    conda)
      if [[ -z "$conda_env" ]]; then echo "--conda-env is required for --mode conda" >&2; exit 2; fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python); py_cmd_human="${py_cmd[*]}" ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python); py_cmd_human="${py_cmd[*]}" ;;
    system)
      py_cmd=(python); py_cmd_human="python" ;;
    *) echo "Unknown --mode: $mode" >&2; usage; exit 2 ;;
  esac
fi

stage_status="failure"
failure_category="unknown"
exit_code=1
skip_reason="unknown"
install_attempted=0
install_cmd=""

assets_json='{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}'

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  echo "{}" >"$out_json"
  echo "{}" >"$analysis_json"
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "${py_cmd_human} -c 'import sys'",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": $assets_json,
  "meta": {
    "python": "",
    "git_commit": "$(git rev-parse HEAD 2>/dev/null || true)",
    "env_vars": {"PY_CMD": "$py_cmd_human", "MODE": "$mode"},
    "decision_reason": "Failed to execute selected python interpreter."
  },
  "failure_category": "deps",
  "error_excerpt": "$(tail -n 220 "$log_file" 2>/dev/null || true)"
}
JSON
  exit 1
fi

export PYRIGHT_PY="$("${py_cmd[@]}" -c 'import sys,platform; print(f"{sys.executable} ({platform.python_version()})")' 2>/dev/null || true)"
export PY_CMD="$py_cmd_human"
export MODE="$mode"

project_args=()
targets=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="Using pyrightconfig.json (--project pyrightconfig.json)."
elif [[ -f "pyproject.toml" ]]; then
  has_tool_pyright=0
  if command -v rg >/dev/null 2>&1; then
    rg -n "^[[:space:]]*\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1 && has_tool_pyright=1
  else
    grep -nE "^[[:space:]]*\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1 && has_tool_pyright=1
  fi
  if [[ "$has_tool_pyright" -eq 1 ]]; then
    project_args=(--project pyproject.toml)
    decision_reason="Using [tool.pyright] in pyproject.toml (--project pyproject.toml)."
  fi
elif [[ -d "src" ]] && find src -type f -name "*.py" -print -quit >/dev/null 2>&1; then
  targets=(src)
  [[ -d "tests" ]] && find tests -type f -name "*.py" -print -quit >/dev/null 2>&1 && targets+=(tests)
  decision_reason="Using src/ layout as Pyright target(s)."
else
  mapfile -t targets < <("${py_cmd[@]}" - <<'PY'
import pathlib

root = pathlib.Path(".").resolve()
exclude = {".git", ".venv", "venv", "__pycache__", "build_output", "dist", "build", ".mypy_cache", ".pytest_cache", "node_modules"}

pkgs = []
for child in root.iterdir():
    if not child.is_dir():
        continue
    if child.name in exclude:
        continue
    try:
        if any(p.name == "__init__.py" for p in child.rglob("__init__.py")):
            pkgs.append(child.name)
    except Exception:
        continue

for p in sorted(set(pkgs)):
    print(p)
PY
  )
  if [[ ${#targets[@]} -gt 0 ]]; then
    decision_reason="Detected package directories containing __init__.py."
  fi
fi

export DECISION_REASON="$decision_reason"

if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  echo "No pyrightconfig.json, no [tool.pyright] in pyproject.toml, and no python package/src layout detected." >&2
  echo "{}" >"$out_json"
  echo "{}" >"$analysis_json"
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "${py_cmd_human} -m pyright",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": $assets_json,
  "meta": {
    "python": "$PYRIGHT_PY",
    "git_commit": "$(git rev-parse HEAD 2>/dev/null || true)",
    "env_vars": {"PY_CMD": "$py_cmd_human", "MODE": "$mode"},
    "decision_reason": "No python sources detected for Pyright target selection."
  },
  "failure_category": "entrypoint_not_found",
  "error_excerpt": "$(tail -n 220 "$log_file" 2>/dev/null || true)"
}
JSON
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd_human} -m pip install -q pyright"
  export INSTALL_CMD="$install_cmd"
  echo "[pyright] Installing pyright via: $install_cmd"
  if ! "${py_cmd[@]}" -m pip install -q pyright; then
    echo "[pyright] Failed to install pyright." >&2
    echo "{}" >"$out_json"
    echo "{}" >"$analysis_json"
    cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "$install_cmd",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": $assets_json,
  "meta": {
    "python": "$PYRIGHT_PY",
    "git_commit": "$(git rev-parse HEAD 2>/dev/null || true)",
    "env_vars": {"PY_CMD": "$py_cmd_human", "MODE": "$mode"},
    "decision_reason": "pyright missing; attempted pip install but it failed."
  },
  "failure_category": "deps",
  "error_excerpt": "$(tail -n 220 "$log_file" 2>/dev/null || true)"
}
JSON
    exit 1
  fi
fi

export INSTALL_ATTEMPTED="$install_attempted"
export INSTALL_CMD="$install_cmd"

cmd=("${py_cmd[@]}" -m pyright)
cmd+=("${targets[@]}")
cmd+=("${project_args[@]}")
cmd+=(--level "$pyright_level" --outputjson)
cmd+=("${pyright_extra_args[@]}")

echo "[pyright] Running: ${cmd[*]}"
pyright_rc=0
("${cmd[@]}" >"$out_json")
pyright_rc=$?

export PYRIGHT_CMD="${cmd[*]}"
export PYRIGHT_RC="$pyright_rc"

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" MODE="$mode" PY_CMD="$py_cmd_human" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import sys
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path("build_output/pyright/log.txt")

repo_root = pathlib.Path(".").resolve()

def safe_load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

data = safe_load_json(out_json)
diagnostics = data.get("generalDiagnostics", []) or []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
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
missing_package_ratio = (
    f"{missing_packages_count}/{total_imported_packages_count}"
    if total_imported_packages_count > 0
    else "0/0"
)

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

def tail_file(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 512 * 1024)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except Exception:
        return ""

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Stage results.json (includes required fields + metrics)
install_attempted = os.environ.get("INSTALL_ATTEMPTED", "0") == "1"
install_cmd = os.environ.get("INSTALL_CMD", "")
pyright_cmd = os.environ.get("PYRIGHT_CMD", "")
pyright_rc = int(os.environ.get("PYRIGHT_RC", "0"))
decision_reason = os.environ.get("DECISION_REASON", "")
pyright_py = os.environ.get("PYRIGHT_PY", "")

status = "success"
exit_code = 0
failure_category = "unknown"
if not data:
    status = "failure"
    exit_code = 1
    failure_category = "invalid_json"

results = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": pyright_cmd,
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": pyright_py,
        "git_commit": "",
        "env_vars": {"PY_CMD": os.environ.get("PY_CMD", ""), "MODE": os.environ.get("MODE", "")},
        "decision_reason": decision_reason,
        "pyright_install_attempted": install_attempted,
        "pyright_install_command": install_cmd,
        "pyright_exit_code": pyright_rc,
        "files_scanned": files_scanned,
    },
    "missing_packages": missing_packages,
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "metrics": analysis_payload["metrics"],
    "failure_category": failure_category,
    "error_excerpt": tail_file(log_path),
}

try:
    import subprocess as _sp
    results["meta"]["git_commit"] = _sp.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=_sp.STDOUT).strip()
except Exception:
    results["meta"]["git_commit"] = ""

results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
sys.exit(exit_code)
PY
exit $?
