#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository/project to analyze

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes foo)

Notes:
  - Missing imports do NOT make this stage fail; the stage fails only if Pyright
    could not run or produce valid JSON output.
  - If `import pyright` fails, this script attempts:
      <resolved_python> -m pip install -q pyright
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
sys_python="python"

if ! command -v "$sys_python" >/dev/null 2>&1; then
  sys_python="python3"
fi

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

repo_root="$(cd "$repo" && pwd)"
out_dir_abs="$repo_root/$out_dir"
mkdir -p "$out_dir_abs"

log_file="$out_dir_abs/log.txt"
out_json="$out_dir_abs/pyright_output.json"
analysis_json="$out_dir_abs/analysis.json"
results_json="$out_dir_abs/results.json"

: >"$log_file"
exec > >(tee -a "$log_file") 2>&1

cd "$repo_root"
echo "[pyright] repo_root=$repo_root"
echo "[pyright] out_dir=$out_dir_abs"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      if command -v python >/dev/null 2>&1; then
        py_cmd=(python)
      elif command -v python3 >/dev/null 2>&1; then
        py_cmd=(python3)
      else
        echo "python not found in PATH" >&2
        exit 2
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "[pyright] python_cmd=${py_cmd[*]}"

install_attempted=0
install_cmd=""
install_log=""

export OUT_DIR="$out_dir_abs"
export OUT_JSON="$out_json"
export ANALYSIS_JSON="$analysis_json"
export RESULTS_JSON="$results_json"
export LOG_FILE="$log_file"
export MODE="$mode"
export PY_CMD="${py_cmd[*]}"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] failed to run python via: ${py_cmd[*]}" >&2
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  "$sys_python" -B - <<'PY'
import json, os, pathlib, subprocess

results_path = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_FILE"])

def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"

env_keys = [
    "CUDA_VISIBLE_DEVICES",
    "SCIMLOPSBENCH_PYTHON",
    "SCIMLOPSBENCH_REPORT",
    "HF_HOME",
    "HF_AUTH_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "TRANSFORMERS_CACHE",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
    "WANDB_MODE",
]
env_vars = {k: os.environ.get(k, "") for k in env_keys if os.environ.get(k) is not None}

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {"dataset": {"path":"", "source":"", "version":"", "sha256":""}, "model": {"path":"", "source":"", "version":"", "sha256":""}},
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": git_commit(),
        "env_vars": env_vars,
        "decision_reason": "failed to execute selected python interpreter",
        "python_cmd": os.environ.get("PY_CMD",""),
        "mode": os.environ.get("MODE",""),
    },
    "failure_category": "deps",
    "error_excerpt": "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]).strip(),
}
results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] pyright not importable; attempting install: $install_cmd"
  set +e
  install_log="$("${py_cmd[@]}" -m pip install -q pyright 2>&1)"
  install_rc=$?
  set -e
  echo "$install_log" || true
  if [[ $install_rc -ne 0 ]]; then
    echo "[pyright] pyright install failed (rc=$install_rc)"
  fi
fi

pyright_project_args=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  pyright_project_args=(--project "pyrightconfig.json")
  targets=(".")
  decision_reason="pyrightconfig.json detected; using --project pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && grep -Eq '^[[:space:]]*\[tool\.pyright\][[:space:]]*$' "pyproject.toml"; then
  pyright_project_args=(--project "pyproject.toml")
  targets=(".")
  decision_reason="pyproject.toml [tool.pyright] detected; using --project pyproject.toml"
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
  decision_reason="src/ layout detected; running on src (and tests if present)"
else
  mapfile -t pkg_dirs < <(
    find . -maxdepth 2 -type f -name '__init__.py' \
      -not -path './.git/*' \
      -not -path './build_output/*' \
      -not -path './benchmark_assets/*' \
      -not -path './benchmark_scripts/*' \
    | xargs -r -n1 dirname 2>/dev/null \
    | sed 's|^\\./||' \
    | awk 'NF' \
    | sort -u
  )
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="package dir(s) with __init__.py detected; running on top-level package dirs"
  fi
fi

if [[ ${#targets[@]} -eq 0 ]]; then
  echo "[pyright] no targets found (no pyrightconfig/pyproject/src/package dirs)"
  echo '{}' >"$out_json"
"$sys_python" -B - <<'PY'
import json, os, pathlib, subprocess
out_dir = pathlib.Path(os.environ["OUT_DIR"])
analysis_path = out_dir / "analysis.json"
results_path = out_dir / "results.json"
log_path = out_dir / "log.txt"
def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"
env_keys = [
    "CUDA_VISIBLE_DEVICES",
    "SCIMLOPSBENCH_PYTHON",
    "SCIMLOPSBENCH_REPORT",
    "HF_HOME",
    "HF_AUTH_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "TRANSFORMERS_CACHE",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
    "WANDB_MODE",
]
env_vars = {k: os.environ.get(k, "") for k in env_keys if os.environ.get(k) is not None}
analysis = {
  "missing_packages": [],
  "pyright": {},
  "meta": {"targets": [], "reason": "no pyrightconfig/pyproject/src/package dirs found"},
  "metrics": {"missing_packages_count": 0, "total_imported_packages_count": 0, "missing_package_ratio": "0/0"},
}
analysis_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {"dataset": {"path":"", "source":"", "version":"", "sha256":""}, "model": {"path":"", "source":"", "version":"", "sha256":""}},
  "meta": {"python": "", "git_commit": git_commit(), "env_vars": env_vars, "decision_reason": "failed to detect targets"},
  "failure_category": "entrypoint_not_found",
  "error_excerpt": "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]).strip(),
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
}
results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
  exit 1
fi

pyright_cmd_str="${py_cmd[*]} -m pyright ${targets[*]} --level $pyright_level --outputjson ${pyright_project_args[*]} ${pyright_extra_args[*]}"
echo "[pyright] command=$pyright_cmd_str"

: >"$out_json"
set +e
"${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_project_args[@]}" "${pyright_extra_args[@]}" >"$out_json"
pyright_rc=$?
set -e
echo "[pyright] pyright_exit_code=$pyright_rc (ignored for stage status if JSON is valid)"

export OUT_JSON="$out_json"
export ANALYSIS_JSON="$analysis_json"
export RESULTS_JSON="$results_json"
export LOG_FILE="$log_file"
export MODE="$mode"
export PY_CMD="${py_cmd[*]}"
export INSTALL_ATTEMPTED="$install_attempted"
export INSTALL_CMD="$install_cmd"
export INSTALL_LOG="$install_log"
export PYRIGHT_EXIT_CODE="$pyright_rc"
export TARGETS="${targets[*]}"
export PROJECT_ARGS="${pyright_project_args[*]}"
export PYRIGHT_CMD="$pyright_cmd_str"
export DECISION_REASON="${decision_reason:-}"

"${py_cmd[@]}" -B - <<'PY'
import ast
import json
import os
import pathlib
import re
import sys
import subprocess
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_FILE"])

targets = [t for t in os.environ.get("TARGETS", "").split() if t.strip()]
pyright_cmd = os.environ.get("PYRIGHT_CMD", "")

def tail_lines(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:]).strip()

base_assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

analysis_payload = {
    "missing_packages": [],
    "pyright": {},
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": "unknown",
        "env_vars": {},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "targets": targets,
        "project_args": os.environ.get("PROJECT_ARGS", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or "0"),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "install_log_excerpt": (os.environ.get("INSTALL_LOG", "") or "")[-4000:],
    },
    "metrics": {
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    },
}

status = "success"
failure_category = "unknown"

def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"

env_keys = [
    "CUDA_VISIBLE_DEVICES",
    "SCIMLOPSBENCH_PYTHON",
    "SCIMLOPSBENCH_REPORT",
    "HF_HOME",
    "HF_AUTH_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "TRANSFORMERS_CACHE",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
    "WANDB_MODE",
]
analysis_payload["meta"]["git_commit"] = git_commit()
analysis_payload["meta"]["env_vars"] = {k: os.environ.get(k, "") for k in env_keys if os.environ.get(k) is not None}

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("pyright output JSON top-level is not an object")
    analysis_payload["pyright"] = data
except Exception as e:
    status = "failure"
    failure_category = "deps"
    analysis_payload["meta"]["pyright_json_error"] = f"{type(e).__name__}: {e}"
    analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    results = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": 1,
        "stage": "pyright",
        "task": "check",
        "command": pyright_cmd,
        "timeout_sec": 600,
        "framework": "unknown",
        "assets": base_assets,
        "meta": analysis_payload["meta"],
        "failure_category": failure_category,
        "error_excerpt": tail_lines(log_path),
        "missing_packages_count": 0,
        "total_imported_packages_count": 0,
        "missing_package_ratio": "0/0",
    }
    results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(1)

diagnostics = analysis_payload["pyright"].get("generalDiagnostics", []) or []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import\\s+\\"([^\\"]+)')
missing_packages = set()
for d in missing_diags:
    msg = d.get("message", "")
    m = pattern.search(msg) if isinstance(msg, str) else None
    if m:
        missing_packages.add(m.group(1).split(".")[0])
analysis_payload["missing_packages"] = sorted(missing_packages)

def iter_py_files(paths: list[str]) -> Iterable[pathlib.Path]:
    exclude_parts = {
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
    for p in paths:
        root = pathlib.Path(p)
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in exclude_parts for part in path.parts):
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

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(targets):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(analysis_payload["missing_packages"])
total_imported_packages_count = len(all_imported_packages)
analysis_payload["metrics"] = {
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": f"{missing_packages_count}/{total_imported_packages_count}",
}
analysis_payload["meta"]["files_scanned"] = files_scanned

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")

results = {
    "status": status,
    "skip_reason": "not_applicable",
    "exit_code": 0,
    "stage": "pyright",
    "task": "check",
    "command": pyright_cmd,
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": base_assets,
    "meta": analysis_payload["meta"],
    "failure_category": "unknown",
    "error_excerpt": "",
    **analysis_payload["metrics"],
}
results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(analysis_payload["metrics"]["missing_package_ratio"])
PY
