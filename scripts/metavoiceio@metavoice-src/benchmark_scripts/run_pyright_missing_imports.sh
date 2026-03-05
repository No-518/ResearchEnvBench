#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs:
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --repo <path>                  Repo root (default: auto-detected)
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <n>              Default: 600
  -- <pyright args...>           Extra args passed to Pyright

Notes:
  - Ensures 'pyright' is importable in the selected environment; installs via pip if missing.
  - Non-zero pyright exit does not fail this stage by itself; this stage fails only on setup/runtime errors.
EOF
}

mode="system"
repo=""
out_dir=""
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
timeout_sec="600"
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
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

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="${repo:-"$(cd "$script_dir/.." && pwd)"}"
stage_dir="${out_dir:-"$repo_root/build_output/pyright"}"
mkdir -p "$stage_dir"

log_path="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

# Always create output files, even if we exit early.
touch "$log_path" "$out_json" "$analysis_json" "$results_json"

exec > >(tee -a "$log_path") 2>&1

cd "$repo_root"
echo "[pyright] repo_root=$repo_root"
echo "[pyright] stage_dir=$stage_dir"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then echo "--venv is required for --mode venv" >&2; exit 2; fi
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then echo "--conda-env is required for --mode conda" >&2; exit 2; fi
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
      exit 2
      ;;
  esac
fi

echo "[pyright] python_cmd=${py_cmd[*]}"

install_attempted="0"
install_cmd=""
install_rc="0"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] Failed to run python via: ${py_cmd[*]}" >&2
  # Write failure results
  REPO_ROOT="$repo_root" STAGE_DIR="$stage_dir" TIMEOUT_SEC="$timeout_sec" PYRIGHT_CMD="" python3 - <<'PY' || true
import json, os, pathlib, subprocess, time
repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
stage_dir = pathlib.Path(os.environ["STAGE_DIR"]).resolve()
log_path = stage_dir / "log.txt"
results_path = stage_dir / "results.json"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo_root, text=True, timeout=5).strip()
    except Exception:
        return ""
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": os.environ.get("PYRIGHT_CMD",""),
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC","600")),
  "framework": "unknown",
  "assets": {"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta": {
    "python": "",
    "git_commit": git_commit(),
    "env_vars": {},
    "decision_reason": "Python interpreter for pyright stage could not be executed."
  },
  "failure_category": "deps",
  "error_excerpt": log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]
}
payload["error_excerpt"] = "\n".join(payload["error_excerpt"])
results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted="1"
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] Installing pyright: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  install_rc="$?"
  set -e
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  echo "[pyright] pyright is not importable after installation attempt (rc=$install_rc)" >&2
  # Best-effort: write placeholder pyright_output.json
  printf '%s\n' '{"version":"","generalDiagnostics":[],"summary":{}}' > "$out_json"
  REPO_ROOT="$repo_root" STAGE_DIR="$stage_dir" TIMEOUT_SEC="$timeout_sec" MODE="$mode" PY_CMD="${py_cmd[*]}" \
  INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" INSTALL_RC="$install_rc" PYRIGHT_CMD="" \
    "${py_cmd[@]}" - <<'PY' || true
import json, os, pathlib, re, subprocess, time
repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
stage_dir = pathlib.Path(os.environ["STAGE_DIR"]).resolve()
log_path = stage_dir / "log.txt"
out_json = stage_dir / "pyright_output.json"
analysis_json = stage_dir / "analysis.json"
results_json = stage_dir / "results.json"

def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo_root, text=True, timeout=5).strip()
    except Exception:
        return ""

payload = {
  "missing_packages": [],
  "pyright": {},
  "meta": {
    "mode": os.environ.get("MODE",""),
    "python_cmd": os.environ.get("PY_CMD",""),
    "install_attempted": os.environ.get("INSTALL_ATTEMPTED","0") == "1",
    "install_cmd": os.environ.get("INSTALL_CMD",""),
    "install_rc": int(os.environ.get("INSTALL_RC","0")),
    "decision_reason": "pyright could not be imported/installed in selected environment."
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
  },
}
analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": os.environ.get("PYRIGHT_CMD",""),
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC","600")),
  "framework": "unknown",
  "assets": {"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta": {
    "python": "",
    "git_commit": git_commit(),
    "env_vars": {},
    "decision_reason": payload["meta"]["decision_reason"],
    "pyright_install": {
      "attempted": payload["meta"]["install_attempted"],
      "command": payload["meta"]["install_cmd"],
      "rc": payload["meta"]["install_rc"],
    },
  },
  "failure_category": "deps",
  "error_excerpt": "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]),
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
}
results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

project_args=()
targets=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="Found pyrightconfig.json; running pyright with --project pyrightconfig.json"
elif [[ -f "pyproject.toml" ]]; then
  # Note: the selected interpreter may be Python<3.11 (no stdlib tomllib), so don't rely on it here.
  if grep -qE '^[[:space:]]*\[tool\.pyright\][[:space:]]*$' pyproject.toml; then
    project_args=(--project pyproject.toml)
    decision_reason="Found [tool.pyright] in pyproject.toml; running pyright with --project pyproject.toml"
  fi
fi

if [[ ${#project_args[@]} -eq 0 ]]; then
  if [[ -d "src" ]]; then
    targets=("src")
    [[ -d "tests" ]] && targets+=("tests")
    decision_reason="Detected src/ layout; running pyright on src (and tests if present)"
  else
    # Detect top-level package dirs: <dir>/__init__.py at repo root
    mapfile -t pkgs < <(find . -maxdepth 2 -type f -name "__init__.py" \
      ! -path "./.venv/*" ! -path "./venv/*" ! -path "./.git/*" \
      ! -path "./build_output/*" ! -path "./benchmark_*/*" \
      -print | sed -n 's|^\\./\\([^/]*\\)/__init__\\.py$|\\1|p' | sort -u)
    if [[ ${#pkgs[@]} -gt 0 ]]; then
      targets=("${pkgs[@]}")
      decision_reason="Detected package dirs via __init__.py: ${pkgs[*]}"
    fi
  fi
fi

if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  # Last resort: still run pyright against the repo root so the benchmark stage is reproducible even
  # for non-standard layouts (single-file scripts, namespace pkgs, missing __init__.py, etc.).
  targets=(".")
  decision_reason="Fallback: could not determine pyright targets from config/layout; running pyright on repository root."
fi

pyright_cmd=("${py_cmd[@]}" -m pyright)
if [[ ${#project_args[@]} -gt 0 ]]; then
  pyright_cmd+=("${project_args[@]}")
else
  pyright_cmd+=("${targets[@]}")
fi
pyright_cmd+=(--level "$pyright_level" --outputjson)
if [[ ${#pyright_extra_args[@]} -gt 0 ]]; then
  pyright_cmd+=("${pyright_extra_args[@]}")
fi

echo "[pyright] decision_reason=$decision_reason"
echo "[pyright] running: ${pyright_cmd[*]}"

pyright_exit="0"
set +e
if command -v timeout >/dev/null 2>&1; then
  timeout "${timeout_sec}s" "${pyright_cmd[@]}" > "$out_json"
  pyright_exit="$?"
else
  "${pyright_cmd[@]}" > "$out_json"
  pyright_exit="$?"
fi
set -e
echo "[pyright] pyright_exit=$pyright_exit (ignored for stage status)"

if [[ ! -s "$out_json" ]]; then
  printf '%s\n' '{"version":"","generalDiagnostics":[],"summary":{}}' > "$out_json"
fi

REPO_ROOT="$repo_root" STAGE_DIR="$stage_dir" OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
MODE="$mode" PY_CMD="${py_cmd[*]}" INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" INSTALL_RC="$install_rc" \
PYRIGHT_CMD="${pyright_cmd[*]}" PYRIGHT_EXIT="$pyright_exit" TIMEOUT_SEC="$timeout_sec" DECISION_REASON="$decision_reason" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
from typing import Iterable

repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
stage_dir = pathlib.Path(os.environ["STAGE_DIR"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"]).resolve()
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"]).resolve()
results_json = pathlib.Path(os.environ["RESULTS_JSON"]).resolve()
log_path = stage_dir / "log.txt"

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, timeout=5).strip()
    except Exception:
        return ""

raw = {}
try:
    raw = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    raw = {"version": "", "generalDiagnostics": [], "summary": {}}

diagnostics = raw.get("generalDiagnostics", []) if isinstance(raw, dict) else []
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
    "pyright": raw,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "install_rc": int(os.environ.get("INSTALL_RC", "0")),
        "pyright_cmd": os.environ.get("PYRIGHT_CMD", ""),
        "pyright_exit": int(os.environ.get("PYRIGHT_EXIT", "0")),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Stage results.json with required fields + metrics
status = "success"
exit_code = 0
failure_category = "unknown"

results_payload = {
    "status": status,
    "skip_reason": "not_applicable",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "600")),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": "",
        "git_commit": git_commit(),
        "env_vars": {},
        "decision_reason": analysis_payload["meta"]["decision_reason"],
        "pyright_install": {
            "attempted": analysis_payload["meta"]["install_attempted"],
            "command": analysis_payload["meta"]["install_cmd"],
            "rc": analysis_payload["meta"]["install_rc"],
        },
        "pyright_exit": analysis_payload["meta"]["pyright_exit"],
    },
    "failure_category": failure_category,
    "error_excerpt": "",
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

exit 0
