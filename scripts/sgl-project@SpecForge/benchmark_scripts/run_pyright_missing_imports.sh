#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Required:
  --repo <path>                  Repository root to analyze

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report (default)

Optional:
  --report-path <path>           Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonpath src)

Outputs (always created):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
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

if [[ -z "$repo" ]]; then
  echo "--repo is required" >&2
  usage
  exit 2
fi

repo="$(cd "$repo" && pwd)"
cd "$repo"

mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

# Ensure mandatory outputs exist even on early failures.
printf '{}' > "$out_json"
printf '{}' > "$analysis_json"

host_python="$(command -v python3 || command -v python || true)"
if [[ -z "$host_python" ]]; then
  echo "[pyright] ERROR: python3/python not found in PATH (needed for JSON/result emission)" >> "$log_path"
  # Minimal results.json fallback (without env introspection).
  cat >"$results_json" <<EOF
{"status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"pyright","task":"check","command":"","timeout_sec":600,"framework":"unknown","assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},"meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"host python not found"},"failure_category":"deps","error_excerpt":"host python not found"}
EOF
  exit 1
fi

resolve_python_from_report() {
  local rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
  "$host_python" - "$rp" <<PY 2>/dev/null || return 1
import json, os, sys
rp = sys.argv[1]
try:
    with open(rp, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
pp = data.get("python_path")
if isinstance(pp, str) and pp.strip():
    print(pp)
    sys.exit(0)
sys.exit(1)
PY
}

py_cmd=()
python_resolution="unknown"

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli"
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      python_resolution="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution="uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_resolution="conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_resolution="poetry"
      ;;
    system)
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
        python_resolution="env:SCIMLOPSBENCH_PYTHON"
      else
        resolved="$(resolve_python_from_report || true)"
        if [[ -n "$resolved" ]]; then
          py_cmd=("$resolved")
          python_resolution="report:python_path"
        else
          # Per runner spec: if report is missing/invalid and no --python, this stage must fail.
          py_cmd=()
          python_resolution="missing_report"
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

install_attempted=0
install_cmd=""
pyright_exit_code=""
decision_reason=""
failure_category=""
force_status=""
pyright_cmd_str=""

{
  echo "[pyright] repo=$repo"
  echo "[pyright] mode=$mode python_resolution=$python_resolution"
  echo "[pyright] out_dir=$out_dir"
} >>"$log_path"

if [[ "${#py_cmd[@]}" -eq 0 ]]; then
  force_status="failure"
  failure_category="missing_report"
else
  if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_path" 2>&1; then
    force_status="failure"
    failure_category="path_hallucination"
  fi
fi

pyright_targets=()
if [[ -z "$force_status" ]]; then
  if [[ -f "pyrightconfig.json" ]]; then
    pyright_targets=(--project pyrightconfig.json)
    decision_reason="Found pyrightconfig.json; using --project pyrightconfig.json"
  elif [[ -f "pyproject.toml" ]] && grep -Eq '^\[tool\.pyright\]' pyproject.toml; then
    pyright_targets=(--project pyproject.toml)
    decision_reason="Found [tool.pyright] in pyproject.toml; using --project pyproject.toml"
  elif [[ -d "src" ]]; then
    pyright_targets=(src)
    if [[ -d "tests" ]]; then
      pyright_targets+=(tests)
    fi
    decision_reason="Detected src/ layout; targeting src (and tests if present)"
  else
    mapfile -t pkg_dirs < <(
      find . -maxdepth 3 -type f -name '__init__.py' \
        -not -path './.git/*' \
        -not -path './.venv/*' \
        -not -path './venv/*' \
        -not -path './build_output/*' \
        -not -path './benchmark_assets/*' \
        -not -path './benchmark_scripts/*' \
        -print 2>/dev/null \
        | sed -E 's|/[^/]+$||' \
        | sed -E 's|^\./||' \
        | sort -u
    )
    if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
      pyright_targets=("${pkg_dirs[@]}")
      decision_reason="Detected package dirs via __init__.py; targeting: ${pkg_dirs[*]}"
    else
      force_status="failure"
      failure_category="entrypoint_not_found"
      decision_reason="No pyright config found and no src/ or package dirs detected"
    fi
  fi
fi

if [[ -z "$force_status" ]]; then
  if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_path" 2>&1; then
    install_attempted=1
    install_cmd="${py_cmd[*]} -m pip install -q pyright"
    echo "[pyright] pyright not importable; attempting install: $install_cmd" >>"$log_path"
    if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_path" 2>&1; then
      force_status="failure"
      if grep -Eqi 'No module named pip|pip.*not found|Could not find a version|command not found' "$log_path"; then
        failure_category="deps"
      else
        failure_category="download_failed"
      fi
    fi
  fi
fi

if [[ -z "$force_status" ]]; then
  pyright_cmd=("${py_cmd[@]}" -m pyright "${pyright_targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}")
  pyright_cmd_str="$("$host_python" - "${pyright_cmd[@]}" <<'PY'
import shlex, sys
print(" ".join(shlex.quote(c) for c in sys.argv[1:]))
PY
)"
  echo "[pyright] command=$pyright_cmd_str" >>"$log_path"
  set +e
  "${pyright_cmd[@]}" >"$out_json" 2>>"$log_path"
  pyright_exit_code="$?"
  set -e
  echo "[pyright] pyright_exit_code=$pyright_exit_code (non-zero does not fail stage)" >>"$log_path"
fi

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" LOG_PATH="$log_path" \
MODE="$mode" PY_CMD="${py_cmd[*]:-}" PYRIGHT_CMD="$pyright_cmd_str" PYRIGHT_EXIT_CODE="${pyright_exit_code:-}" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" PYTHON_RESOLUTION="$python_resolution" \
DECISION_REASON="$decision_reason" FORCE_STATUS="$force_status" FAILURE_CATEGORY="$failure_category" \
PYRIGHT_LEVEL="$pyright_level" REPO_ROOT="$repo" \
  "$host_python" - <<'PY'
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
log_path = pathlib.Path(os.environ["LOG_PATH"])

force_status = os.environ.get("FORCE_STATUS", "").strip() or None
failure_category = os.environ.get("FAILURE_CATEGORY", "").strip() or "unknown"

def tail_excerpt(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])

def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=10)
        return out.strip()
    except Exception:
        return ""

def safe_env() -> dict:
    keep_prefixes = ("SCIMLOPSBENCH_", "CUDA_", "HF_", "TRANSFORMERS_", "TORCH", "PYTHON")
    keep_keys = {"PATH","HOME","USER","SHELL","PWD","VIRTUAL_ENV","CONDA_DEFAULT_ENV","CONDA_PREFIX"}
    out = {}
    for k, v in os.environ.items():
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes):
            out[k] = v
    return out

def iter_py_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
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
        "benchmark_scripts",
    }
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

status = "success" if force_status != "failure" else "failure"

pyright_data = {}
try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    if status != "failure":
        status = "failure"
        failure_category = "invalid_json"
    pyright_data = {}

diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

all_imported_packages = set()
files_scanned = 0
for py_file in iter_py_files(repo_root):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = (
    f"{missing_packages_count}/{total_imported_packages_count}" if total_imported_packages_count else "0/0"
)

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": pyright_data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
        "pyright_cmd": os.environ.get("PYRIGHT_CMD", ""),
        "pyright_exit_code": os.environ.get("PYRIGHT_EXIT_CODE", ""),
        "pyright_level": os.environ.get("PYRIGHT_LEVEL", ""),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")),
        "install_command": os.environ.get("INSTALL_CMD", ""),
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": 0 if status != "failure" else 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", "") or os.environ.get("PY_CMD", ""),
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
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": git_commit(),
        "env_vars": safe_env(),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or "0")),
        "pyright_install_command": os.environ.get("INSTALL_CMD", ""),
        "pyright_exit_code": os.environ.get("PYRIGHT_EXIT_CODE", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
    },
    "failure_category": failure_category if status == "failure" else "unknown",
    "error_excerpt": tail_excerpt(log_path),
}

results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
raise SystemExit(results["exit_code"])
PY
