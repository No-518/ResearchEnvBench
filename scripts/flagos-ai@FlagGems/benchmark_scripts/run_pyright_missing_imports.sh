#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (always written):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Repository root to analyze

Python environment selection (pick ONE):
  --python <path>                Explicit python executable (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH (default)

Optional:
  --out-dir <path>               Stage output dir (default: build_output/pyright)
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright

Notes:
  - If pyright is missing in the selected environment, this script attempts:
      "<python>" -m pip install -q pyright
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
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
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

repo="$(cd "$repo" && pwd)"
mkdir -p "$out_dir"

log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec > >(tee "$log_path") 2>&1

cd "$repo"

mkdir -p "benchmark_assets/cache/pip" "benchmark_assets/cache/xdg"
export PIP_CACHE_DIR="$repo/benchmark_assets/cache/pip"
export XDG_CACHE_HOME="$repo/benchmark_assets/cache/xdg"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

install_attempted=0
install_cmd=""
install_failed=0
install_failure_category="deps"

echo "[pyright] Using python: ${py_cmd[*]}"
if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' ; then
  echo "[pyright] Failed to run python via: ${py_cmd[*]}" >&2
  # Write minimal results.json
  "${py_cmd[@]}" - <<'PY' || true
import json, os, sys
out_dir = os.environ.get("OUT_DIR", "build_output/pyright")
os.makedirs(out_dir, exist_ok=True)
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":sys.executable,"git_commit":"","env_vars":{},"decision_reason":"python invocation failed"},
  "failure_category":"deps",
  "error_excerpt":"python invocation failed"
}
with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
  json.dump(payload, f, indent=2)
PY
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] pyright not found; attempting install: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    install_failed=1
    # Heuristic classification.
    install_failure_category="deps"
    if command -v rg >/dev/null 2>&1; then
      if rg -n "Temporary failure|Name or service not known|Connection|timed out|CERTIFICATE|No matching distribution" "$log_path" >/dev/null 2>&1; then
        install_failure_category="download_failed"
      fi
    else
      if grep -E "Temporary failure|Name or service not known|Connection|timed out|CERTIFICATE|No matching distribution" "$log_path" >/dev/null 2>&1; then
        install_failure_category="download_failed"
      fi
    fi
  fi
fi

# Determine pyright targets according to priority rules.
project_arg=()
targets=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_arg=(--project pyrightconfig.json)
  decision_reason="Found pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_arg=(--project pyproject.toml)
  decision_reason="Found [tool.pyright] in pyproject.toml"
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
  decision_reason="Detected src/ layout"
else
  mapfile -t pkg_dirs < <(find . -type f -name "__init__.py" -not -path "./.git/*" -not -path "./build_output/*" -not -path "./benchmark_assets/*" -not -path "./.venv/*" -not -path "./venv/*" 2>/dev/null | sed 's#/__init__\\.py$##' | sort -u)
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="Detected package dirs via __init__.py"
  fi
fi

if [[ $install_failed -eq 1 ]]; then
  echo "[pyright] Failed to install pyright"
  # Still attempt to write empty pyright_output.json for downstream parsing.
  echo '{}' > "$out_json"
  "${py_cmd[@]}" - <<PY || true
import json, os, sys
out_dir = ${out_dir@Q}
analysis_json = os.path.join(out_dir, "analysis.json")
results_json = os.path.join(out_dir, "results.json")
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":${install_cmd@Q},
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python":sys.executable,
    "git_commit":"",
    "env_vars":{},
    "decision_reason":"pyright install failed",
    "pyright_install":{"attempted":True,"command":${install_cmd@Q},"returncode":1}
  },
  "failure_category":${install_failure_category@Q},
  "error_excerpt":"pyright install failed; see log.txt"
}
with open(analysis_json, "w", encoding="utf-8") as f:
  json.dump({"missing_packages":[],"pyright":{},"meta":payload["meta"],"metrics":{}}, f, indent=2)
with open(results_json, "w", encoding="utf-8") as f:
  json.dump(payload, f, indent=2)
PY
  exit 1
fi

if [[ ${#project_arg[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  echo "[pyright] No pyright project/targets found; failing."
  echo '{}' > "$out_json"
  "${py_cmd[@]}" - <<PY || true
import json, os, sys
out_dir = ${out_dir@Q}
analysis_json = os.path.join(out_dir, "analysis.json")
results_json = os.path.join(out_dir, "results.json")
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":sys.executable,"git_commit":"","env_vars":{},"decision_reason":"No targets detected"},
  "failure_category":"entrypoint_not_found",
  "error_excerpt":"No pyrightconfig.json, no [tool.pyright], no src/, and no __init__.py packages detected."
}
with open(analysis_json, "w", encoding="utf-8") as f:
  json.dump({"missing_packages":[],"pyright":{},"meta":payload["meta"],"metrics":{}}, f, indent=2)
with open(results_json, "w", encoding="utf-8") as f:
  json.dump(payload, f, indent=2)
PY
  exit 1
fi

cmd_str=""
if [[ ${#project_arg[@]} -gt 0 ]]; then
  cmd_str="pyright ${project_arg[*]}"
  echo "[pyright] Running: ${py_cmd[*]} -m pyright --outputjson --level $pyright_level ${project_arg[*]} ${pyright_extra_args[*]}"
  set +e
  "${py_cmd[@]}" -m pyright --outputjson --level "$pyright_level" "${project_arg[@]}" "${pyright_extra_args[@]}" > "$out_json"
  pyright_rc=$?
  set -e
else
  cmd_str="pyright ${targets[*]}"
  echo "[pyright] Running: ${py_cmd[*]} -m pyright --outputjson --level $pyright_level ${targets[*]} ${pyright_extra_args[*]}"
  set +e
  "${py_cmd[@]}" -m pyright --outputjson --level "$pyright_level" "${targets[@]}" "${pyright_extra_args[@]}" > "$out_json"
  pyright_rc=$?
  set -e
fi

# Always produce analysis/results JSON even if pyright exits non-zero.
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" MODE="$mode" PY_CMD="${py_cmd[*]}" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" PYRIGHT_RC="$pyright_rc" DECISION_REASON="$decision_reason" CMD_STR="$cmd_str" LOG_PATH="$log_path" \
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

repo_root = pathlib.Path(".").resolve()

def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

data = {}
try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

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
    if total_imported_packages_count
    else "0/0"
)

status = "success"
failure_category = "unknown"
exit_code = 0
if files_scanned == 0:
    status = "failure"
    failure_category = "entrypoint_not_found"
    exit_code = 1

install_attempted = os.environ.get("INSTALL_ATTEMPTED", "0") == "1"
install_cmd = os.environ.get("INSTALL_CMD", "")
pyright_rc = int(os.environ.get("PYRIGHT_RC", "0") or 0)

meta = {
    "mode": os.environ.get("MODE", ""),
    "python_cmd": os.environ.get("PY_CMD", ""),
    "files_scanned": files_scanned,
    "decision_reason": os.environ.get("DECISION_REASON", ""),
    "pyright_install": {
        "attempted": install_attempted,
        "command": install_cmd,
    },
    "pyright_returncode": pyright_rc,
}

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": meta,
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
    "command": os.environ.get("CMD_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": sys.executable,
        "git_commit": "",
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install": meta["pyright_install"],
        "pyright_returncode": pyright_rc,
    },
    "metrics": analysis_payload["metrics"],
    "missing_packages": missing_packages,
    "failure_category": failure_category,
    "error_excerpt": "",
}

if status == "failure":
    results_payload["error_excerpt"] = tail_text(pathlib.Path(os.environ.get("LOG_PATH", "build_output/pyright/log.txt")))

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(missing_package_ratio)
PY

exit 0
