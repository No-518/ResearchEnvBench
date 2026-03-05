#!/usr/bin/env bash
set -euo pipefail

stage="pyright"
task="check"
timeout_sec="${SCIMLOPSBENCH_PYRIGHT_TIMEOUT_SEC:-600}"

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (always written):
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
  --mode system                  Use: report.json python_path (default) or python from PATH if report python invalid

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright (e.g. --project pyrightconfig.json)
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

repo_root="$(cd "$repo" && pwd)"
mkdir -p "${repo_root}/${out_dir}"
log_file="${repo_root}/${out_dir}/log.txt"
out_json="${repo_root}/${out_dir}/pyright_output.json"
analysis_json="${repo_root}/${out_dir}/analysis.json"
results_json="${repo_root}/${out_dir}/results.json"

exec >"${log_file}" 2>&1
echo "[pyright] timestamp_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "[pyright] repo_root=${repo_root}"

git_commit="$(git -C "${repo_root}" rev-parse HEAD 2>/dev/null || true)"

sys_python="$(command -v python3 || command -v python || true)"
if [[ -z "${sys_python}" ]]; then
  echo "[pyright] ERROR: python3/python not found in PATH (needed to write results.json)"
  exit 1
fi

status="failure"
failure_category="unknown"
skip_reason="not_applicable"
exit_code=1
pyright_cmd_str=""
install_attempted=0
install_cmd=""
install_rc=""
decision_reason=""
py_cmd_serialized=""

finalize() {
  local rc="$1"
  if [[ "${rc}" -eq 0 && "${status}" != "failure" ]]; then
    exit_code=0
  else
    status="failure"
    exit_code=1
  fi

  # Ensure required files exist even on early failure.
  if [[ ! -f "${out_json}" ]]; then
    echo "{}" >"${out_json}"
  fi
  if [[ ! -f "${analysis_json}" ]]; then
    echo "{}" >"${analysis_json}"
  fi

  error_excerpt="$(tail -n 220 "${log_file}" || true)"

  STATUS="${status}" SKIP_REASON="${skip_reason}" EXIT_CODE="${exit_code}" PYRIGHT_CMD="${pyright_cmd_str}" TIMEOUT_SEC="${timeout_sec}" \
  PYTHON_BIN="${python_bin}" GIT_COMMIT="${git_commit}" MODE="${mode}" PY_CMD_SERIALIZED="${py_cmd_serialized}" \
  INSTALL_ATTEMPTED="${install_attempted}" INSTALL_CMD="${install_cmd}" INSTALL_RC="${install_rc}" DECISION_REASON="${decision_reason}" \
  FAILURE_CATEGORY="${failure_category}" ERROR_EXCERPT="${error_excerpt}" ANALYSIS_JSON="${analysis_json}" RESULTS_JSON="${results_json}" \
  "${sys_python}" - <<'PY' || true
import json
import os
import time
from pathlib import Path

analysis_json = Path(os.environ["ANALYSIS_JSON"])
results_json = Path(os.environ["RESULTS_JSON"])

payload = {
  "status": os.environ.get("STATUS", "failure"),
  "skip_reason": os.environ.get("SKIP_REASON", "not_applicable"),
  "exit_code": int(os.environ.get("EXIT_CODE", "1")),
  "stage": "pyright",
  "task": "check",
  "command": os.environ.get("PYRIGHT_CMD", ""),
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "0")),
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": os.environ.get("PYTHON_BIN", ""),
    "git_commit": os.environ.get("GIT_COMMIT", ""),
    "env_vars": {
      "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
      "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", "")
    },
    "decision_reason": os.environ.get("DECISION_REASON", ""),
    "mode": os.environ.get("MODE", ""),
    "py_cmd": os.environ.get("PY_CMD_SERIALIZED", ""),
    "pyright_install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
    "pyright_install_cmd": os.environ.get("INSTALL_CMD", ""),
    "pyright_install_rc": os.environ.get("INSTALL_RC", ""),
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
  "error_excerpt": os.environ.get("ERROR_EXCERPT", "")
}

# Merge metrics from analysis.json if present.
try:
  analysis = json.loads(analysis_json.read_text(encoding="utf-8"))
  metrics = analysis.get("metrics") if isinstance(analysis, dict) else None
  if isinstance(metrics, dict):
    payload.update({
      "missing_packages_count": metrics.get("missing_packages_count"),
      "total_imported_packages_count": metrics.get("total_imported_packages_count"),
      "missing_package_ratio": metrics.get("missing_package_ratio"),
    })
except Exception:
  pass

results_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}
trap 'finalize $?' EXIT

cd "${repo_root}"

py_cmd=()
if [[ -n "${python_bin}" ]]; then
  py_cmd=("${python_bin}")
else
  case "${mode}" in
    venv)
      if [[ -z "${venv_dir}" ]]; then
        echo "[pyright] ERROR: --venv is required for --mode venv"
        failure_category="args_unknown"
        decision_reason="User selected --mode venv but did not provide --venv."
        exit 1
      fi
      py_cmd=("${venv_dir}/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("${venv_dir}/bin/python")
      ;;
    conda)
      if [[ -z "${conda_env}" ]]; then
        echo "[pyright] ERROR: --conda-env is required for --mode conda"
        failure_category="args_unknown"
        decision_reason="User selected --mode conda but did not provide --conda-env."
        exit 1
      fi
      if ! command -v conda >/dev/null 2>&1; then
        echo "[pyright] ERROR: conda not found in PATH"
        failure_category="deps"
        decision_reason="User selected --mode conda but conda is not available."
        exit 1
      fi
      py_cmd=(conda run -n "${conda_env}" python)
      ;;
    poetry)
      if ! command -v poetry >/dev/null 2>&1; then
        echo "[pyright] ERROR: poetry not found in PATH"
        failure_category="deps"
        decision_reason="User selected --mode poetry but poetry is not available."
        exit 1
      fi
      py_cmd=(poetry run python)
      ;;
    system)
      # Resolve python with runner-style rules:
      # 1) SCIMLOPSBENCH_PYTHON
      # 2) report.json python_path
      # 3) fallback python from PATH (only if report exists but python_path is missing/invalid)
      resolved="$("${sys_python}" - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path("benchmark_scripts").resolve()))
import runner

report_path = runner.resolve_report_path(None)
res, err = runner.resolve_python(cli_python=None, report_path=report_path)
if res is None:
    print("", end="")
    raise SystemExit(1)
print(res.python, end="")
PY
      )" || true
      if [[ -n "${resolved}" ]]; then
        py_cmd=("${resolved}")
      else
        # This happens if report.json is missing/invalid and SCIMLOPSBENCH_PYTHON not set.
        echo "[pyright] ERROR: Failed to resolve python from report.json and SCIMLOPSBENCH_PYTHON is not set."
        failure_category="missing_report"
        decision_reason="Pyright stage requires a configured python; default resolution uses report.json python_path."
        exit 1
      fi
      ;;
    *)
      echo "[pyright] ERROR: Unknown --mode: ${mode}"
      failure_category="args_unknown"
      decision_reason="Unsupported --mode value."
      exit 2
      ;;
  esac
fi

if [[ -z "${python_bin}" && "${#py_cmd[@]}" -eq 1 ]]; then
  python_bin="${py_cmd[0]}"
fi

py_cmd_serialized="$(printf '%q ' "${py_cmd[@]}")"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] ERROR: Failed to run python via: ${py_cmd_serialized}"
  failure_category="deps"
  decision_reason="Selected python command is not runnable."
  exit 1
fi

# Ensure pyright is importable in the selected environment; attempt install if missing.
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  if "${py_cmd[@]}" -m pip --version >/dev/null 2>&1; then
    install_cmd="${py_cmd_serialized}-m pip install -q pyright"
    set +e
    "${py_cmd[@]}" -m pip install -q pyright
    install_rc="$?"
    set -e
    if [[ "${install_rc}" -ne 0 ]]; then
      echo "[pyright] ERROR: pip install pyright failed (rc=${install_rc})"
      failure_category="download_failed"
      decision_reason="Pyright was missing; attempted pip install in the selected environment."
      exit 1
    fi
  else
    echo "[pyright] ERROR: pip is not available in the selected environment"
    failure_category="deps"
    decision_reason="Pyright was missing and pip is unavailable."
    exit 1
  fi
fi

# Determine what to analyze (do not always run on '.').
project_args=()
targets=()
if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project "pyrightconfig.json")
  decision_reason="Found pyrightconfig.json; using --project pyrightconfig.json."
elif [[ -f "pyproject.toml" ]] && grep -qE '^\\[tool\\.pyright\\]' "pyproject.toml" >/dev/null 2>&1; then
  project_args=(--project "pyproject.toml")
  decision_reason="Found [tool.pyright] in pyproject.toml; using --project pyproject.toml."
elif [[ -d "src" ]]; then
  targets=("src")
  if [[ -d "tests" ]]; then
    targets+=("tests")
  fi
  decision_reason="Detected src/ layout; targeting src (and tests if present)."
else
  # Prefer common python code dirs when present.
  if [[ -d "end-to-end" ]] && compgen -G "end-to-end/*.py" >/dev/null 2>&1; then
    targets+=("end-to-end")
  fi
  if [[ -d "gr-tempest/python" ]] && compgen -G "gr-tempest/python/*.py" >/dev/null 2>&1; then
    targets+=("gr-tempest/python")
  fi
  if [[ -d "text_generation" ]] && compgen -G "text_generation/*.py" >/dev/null 2>&1; then
    targets+=("text_generation")
  fi
  if [[ "${#targets[@]}" -gt 0 ]]; then
    decision_reason="Detected python code directories; targeting: ${targets[*]}."
  else
    # Fallback: directories containing __init__.py (excluding docs/build outputs).
    mapfile -t targets < <(
      find . -name "__init__.py" -type f \
        | sed 's|^\\./||' \
        | grep -Ev '^(\\.git/|build_output/|benchmark_assets/|benchmark_scripts/|\\.venv/|venv/|dist/|build/|node_modules/|gr-tempest/docs/)' \
        | xargs -r -n1 dirname \
        | sort -u
    )
    if [[ "${#targets[@]}" -gt 0 ]]; then
      decision_reason="Detected package directories via __init__.py; targeting: ${targets[*]}."
    fi
  fi
fi

if [[ "${#project_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  echo "[pyright] ERROR: Could not determine what to analyze (no pyrightconfig/pyproject/src/package dirs found)."
  failure_category="entrypoint_not_found"
  decision_reason="No pyrightconfig.json, no pyproject.toml [tool.pyright], no src/, and no detectable python package/code directories."
  exit 1
fi

echo "[pyright] decision_reason=${decision_reason}"

pyright_cmd=("${py_cmd[@]}" -m pyright)
if [[ "${#targets[@]}" -gt 0 ]]; then
  pyright_cmd+=("${targets[@]}")
fi
pyright_cmd+=(--level "${pyright_level}" --outputjson)
if [[ "${#project_args[@]}" -gt 0 ]]; then
  pyright_cmd+=("${project_args[@]}")
fi
if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
  pyright_cmd+=("${pyright_extra_args[@]}")
fi

pyright_cmd_str="$(printf '%q ' "${pyright_cmd[@]}")"
echo "[pyright] cmd=${pyright_cmd_str}"

# Always produce JSON output even if Pyright returns non-zero.
set +e
"${pyright_cmd[@]}" >"${out_json}"
pyright_rc="$?"
set -e
echo "[pyright] pyright_rc=${pyright_rc} (ignored for stage status)"

# Post-process results with the same interpreter.
OUT_JSON="${out_json}" ANALYSIS_JSON="${analysis_json}" RESULTS_JSON="${results_json}" \
MODE="${mode}" PY_CMD="${py_cmd_serialized}" TARGETS="${targets[*]}" PROJECT_ARGS="${project_args[*]}" \
"${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

targets = [t for t in os.environ.get("TARGETS", "").split() if t]
if not targets:
    targets = ["."]

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
        src = py_file.read_text(encoding="utf-8", errors="ignore")
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

data = {}
try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^"]+)"')
missing_packages = sorted(
    {pattern.search(d.get("message", "")).group(1).split(".")[0] for d in missing_diags if pattern.search(d.get("message", ""))}
)

all_imported_packages = set()
files_scanned = 0
for t in targets:
    root = pathlib.Path(t)
    if not root.exists():
        continue
    for py_file in iter_py_files(root):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "targets": targets,
        "project_args": os.environ.get("PROJECT_ARGS", ""),
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
results_json.write_text(json.dumps(payload["metrics"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

status="success"
failure_category="not_applicable"
exit_code=0
