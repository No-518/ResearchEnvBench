#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (reportMissingImports).

Outputs (under build_output/pyright by default):
  log.txt
  pyright_output.json
  analysis.json
  results.json

Required:
  --repo <path>                  Path to the repository/project to analyze

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use python from agent report (default), with PATH fallback

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --report-path <path>           Agent report path (default: /opt/scimlopsbench/report.json)
  -- <pyright args...>           Extra args passed to Pyright
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
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pyright_extra_args=("$@"); break ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${repo}" ]]; then
  echo "--repo is required" >&2
  usage
  exit 2
fi

PY_JSON="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"

REPO_ROOT="$(cd "${repo}" && pwd)"
OUT_DIR="${REPO_ROOT}/${out_dir}"
mkdir -p "${OUT_DIR}"
LOG_PATH="${OUT_DIR}/log.txt"
PYRIGHT_OUT_JSON="${OUT_DIR}/pyright_output.json"
ANALYSIS_JSON="${OUT_DIR}/analysis.json"
RESULTS_JSON="${OUT_DIR}/results.json"

: >"${LOG_PATH}"
echo "{}" > "${PYRIGHT_OUT_JSON}"
echo "{}" > "${ANALYSIS_JSON}"
exec > >(tee -a "${LOG_PATH}") 2>&1

status="failure"
skip_reason="unknown"
failure_category="unknown"
exit_code=1
command_str=""

install_attempted=0
install_cmd=""
python_resolution_warning=""
decision_reason=""
wrote_results=0

finalize() {
  local rc=$?
  trap - EXIT
  if [[ "${wrote_results}" -eq 1 ]]; then
    exit "${rc}"
  fi
  local git_commit
  git_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"

  PYRIGHT_STATUS="${status}" \
  PYRIGHT_SKIP_REASON="${skip_reason}" \
  PYRIGHT_EXIT_CODE="${exit_code}" \
  PYRIGHT_FAILURE_CATEGORY="${failure_category}" \
  PYRIGHT_COMMAND_STR="${command_str}" \
  PYRIGHT_INSTALL_ATTEMPTED="${install_attempted}" \
  PYRIGHT_INSTALL_CMD="${install_cmd}" \
  PYRIGHT_PYWARN="${python_resolution_warning}" \
  PYRIGHT_DECISION_REASON="${decision_reason}" \
  PYRIGHT_GIT_COMMIT="${git_commit}" \
  PYRIGHT_LOG_PATH="${LOG_PATH}" \
  PYRIGHT_ANALYSIS_JSON="${ANALYSIS_JSON}" \
  PYRIGHT_OUT_JSON="${PYRIGHT_OUT_JSON}" \
  PYRIGHT_RESULTS_JSON="${RESULTS_JSON}" \
  "${PY_JSON}" - <<'PY'
import json
import os

log_path = os.environ.get("PYRIGHT_LOG_PATH", "")
try:
    tail = "\n".join(
        open(log_path, "r", encoding="utf-8", errors="replace").read().splitlines()[-240:]
    ).strip()
except Exception:
    tail = ""

payload = {
    "status": os.environ.get("PYRIGHT_STATUS", "failure"),
    "skip_reason": os.environ.get("PYRIGHT_SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "1")),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYRIGHT_PYTHON", ""),
        "git_commit": os.environ.get("PYRIGHT_GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "pyright_install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0"))),
        "pyright_install_command": os.environ.get("PYRIGHT_INSTALL_CMD", ""),
        "python_resolution_warning": os.environ.get("PYRIGHT_PYWARN", ""),
        "decision_reason": os.environ.get("PYRIGHT_DECISION_REASON", ""),
        "analysis": os.environ.get("PYRIGHT_ANALYSIS_JSON", ""),
        "pyright_output": os.environ.get("PYRIGHT_OUT_JSON", ""),
    },
    "failure_category": os.environ.get("PYRIGHT_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail,
}

out_path = os.environ.get("PYRIGHT_RESULTS_JSON", "")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
PY

  exit "${exit_code}"
}

trap 'finalize' EXIT

cd "${REPO_ROOT}"

py_cmd=()
if [[ -n "${python_bin}" ]]; then
  py_cmd=("${python_bin}")
else
  case "${mode}" in
    venv)
      [[ -n "${venv_dir}" ]] || { echo "--venv is required for --mode venv" >&2; failure_category="args_unknown"; exit 1; }
      py_cmd=("${venv_dir}/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("${venv_dir}/bin/python")
      ;;
    conda)
      [[ -n "${conda_env}" ]] || { echo "--conda-env is required for --mode conda" >&2; failure_category="args_unknown"; exit 1; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; failure_category="deps"; exit 1; }
      py_cmd=(conda run -n "${conda_env}" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; failure_category="deps"; exit 1; }
      py_cmd=(poetry run python)
      ;;
    system)
      report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
      else
        report_py="$("${PY_JSON}" - <<'PY' "${report_path}" 2>/dev/null || true
import json, sys
try:
  print(json.load(open(sys.argv[1], "r", encoding="utf-8")).get("python_path","") or "")
except Exception:
  print("")
PY
        )"
        if [[ -z "${report_py}" ]]; then
          echo "Missing/invalid report for python resolution: ${report_path}" >&2
          failure_category="missing_report"
          exit 1
        fi
        if [[ ! -x "${report_py}" ]]; then
          python_resolution_warning="report python_path not executable; falling back to PATH python"
          if command -v python3 >/dev/null 2>&1; then
            py_cmd=(python3)
          else
            py_cmd=(python)
          fi
        else
          py_cmd=("${report_py}")
        fi
      fi
      ;;
    *)
      echo "Unknown --mode: ${mode}" >&2
      failure_category="args_unknown"
      exit 1
      ;;
  esac
fi

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  failure_category="deps"
  exit 1
fi

PYRIGHT_PYTHON="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
export PYRIGHT_PYTHON

echo "[pyright] repo_root=${REPO_ROOT}"
echo "[pyright] out_dir=${OUT_DIR}"
echo "[pyright] mode=${mode}"
echo "[pyright] python=${PYRIGHT_PYTHON}"

decision_reason="Detect missing imports via Pyright JSON output; choose targets based on repo structure (pyrightconfig/pyproject/src/packages)."

# Ensure pyright is importable; if not, install it into the selected interpreter env.
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] pyright not importable; attempting install: ${install_cmd}"
  if ! "${py_cmd[@]}" -m pip install -q pyright; then
    echo "[pyright] failed to install pyright" >&2
    failure_category="download_failed"
    exit 1
  fi
fi

project_args=()
targets=()
target_mode=""

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  target_mode="pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  target_mode="pyproject.toml:[tool.pyright]"
elif [[ -d "src" ]]; then
  targets=(src)
  [[ -d "tests" ]] && targets+=(tests)
  target_mode="src_layout"
else
  mapfile -t targets < <("${PY_JSON}" - <<'PY'
import os
from pathlib import Path

root = Path(".").resolve()
exclude = {
  ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".venv", "venv",
  "build", "dist", "node_modules", "build_output", "benchmark_assets",
}

pkg_dirs = set()
for p in root.rglob("__init__.py"):
  parts = set(p.parts)
  if parts & exclude:
    continue
  pkg_dirs.add(str(p.parent.relative_to(root)))

for d in sorted(pkg_dirs):
  print(d)
PY
  )
  target_mode="package_dirs"
fi

if [[ "${#project_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  echo "[pyright] no pyright config and no python targets found" >&2
  failure_category="entrypoint_not_found"
  status="failure"
  exit_code=1
  exit 1
fi

cmd=("${py_cmd[@]}" -m pyright)
cmd+=("${targets[@]}")
cmd+=("--level" "${pyright_level}" "--outputjson")
cmd+=("${project_args[@]}")
cmd+=("${pyright_extra_args[@]}")
command_str="${cmd[*]} > ${PYRIGHT_OUT_JSON}"

echo "[pyright] target_mode=${target_mode}"
echo "[pyright] command=${command_str}"

# Run pyright; non-zero exit due to diagnostics should not fail this stage.
set +e
"${cmd[@]}" > "${PYRIGHT_OUT_JSON}"
pyright_rc=$?
set -e
echo "[pyright] pyright_exit_code=${pyright_rc}"

# Always write analysis.json and results.json.
OUT_JSON="${PYRIGHT_OUT_JSON}" ANALYSIS_JSON="${ANALYSIS_JSON}" RESULTS_JSON="${RESULTS_JSON}" TARGET_MODE="${target_mode}" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable, List, Set

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
target_mode = os.environ.get("TARGET_MODE", "")
repo_root = pathlib.Path(".").resolve()

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import \"([^\"]+)\"')
missing_packages: List[str] = []
for d in missing_diags:
    msg = str(d.get("message", ""))
    m = pattern.search(msg)
    if not m:
        continue
    mod = m.group(1)
    pkg = mod.split(".")[0]
    if pkg and pkg not in missing_packages:
        missing_packages.append(pkg)
missing_packages.sort()

def iter_py_files(roots: List[pathlib.Path]) -> Iterable[pathlib.Path]:
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
    seen: Set[pathlib.Path] = set()
    for r in roots:
        if not r.exists():
            continue
        for p in r.rglob("*.py"):
            if any(part in exclude_dirs for part in p.parts):
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            yield p

def collect_imported_packages(py_file: pathlib.Path) -> Set[str]:
    pkgs: Set[str] = set()
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

# Choose scan roots:
scan_roots: List[pathlib.Path] = []
if (repo_root / "pyrightconfig.json").exists() or (repo_root / "pyproject.toml").exists():
    # When a project file is used, still avoid scanning the entire repo if a src/ layout exists.
    if (repo_root / "src").is_dir():
        scan_roots = [repo_root / "src"]
        if (repo_root / "tests").is_dir():
            scan_roots.append(repo_root / "tests")
    else:
        scan_roots = [repo_root]
else:
    # best-effort: use repo root
    scan_roots = [repo_root]

all_imported_packages: Set[str] = set()
files_scanned = 0
for py_file in iter_py_files(scan_roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = int(len(missing_packages))
total_imported_packages_count = int(len(all_imported_packages))
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "target_mode": target_mode,
        "files_scanned": files_scanned,
        "scan_roots": [str(p) for p in scan_roots],
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2), encoding="utf-8")
results_json.write_text(json.dumps(analysis_payload["metrics"], ensure_ascii=False, indent=2), encoding="utf-8")
print(missing_package_ratio)
PY

# Merge metrics into stage-level results.json schema.
metrics="$("${PY_JSON}" - <<'PY' "${RESULTS_JSON}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(json.dumps(obj))
PY
)"

missing_packages_count="$("${PY_JSON}" - <<'PY' "${RESULTS_JSON}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(obj.get("missing_packages_count", 0))
PY
)"
total_imported_packages_count="$("${PY_JSON}" - <<'PY' "${RESULTS_JSON}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(obj.get("total_imported_packages_count", 0))
PY
)"
missing_package_ratio="$("${PY_JSON}" - <<'PY' "${RESULTS_JSON}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(obj.get("missing_package_ratio", ""))
PY
)"

# This stage is successful if pyright executed and produced JSON, regardless of diagnostics.
status="success"
skip_reason="not_applicable"
failure_category=""
exit_code=0

# Keep the stage-level schema (required fields) while also including metrics at top-level for the summarizer.
git_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"

PYRIGHT_STATUS="${status}" \
PYRIGHT_SKIP_REASON="${skip_reason}" \
PYRIGHT_EXIT_CODE="${exit_code}" \
PYRIGHT_FAILURE_CATEGORY="${failure_category}" \
PYRIGHT_COMMAND_STR="${command_str}" \
PYRIGHT_INSTALL_ATTEMPTED="${install_attempted}" \
PYRIGHT_INSTALL_CMD="${install_cmd}" \
PYRIGHT_PYWARN="${python_resolution_warning}" \
PYRIGHT_DECISION_REASON="${decision_reason}" \
PYRIGHT_GIT_COMMIT="${git_commit}" \
PYRIGHT_LOG_PATH="${LOG_PATH}" \
PYRIGHT_RESULTS_JSON="${RESULTS_JSON}" \
PYRIGHT_MISSING_COUNT="${missing_packages_count}" \
PYRIGHT_IMPORTED_COUNT="${total_imported_packages_count}" \
PYRIGHT_MISSING_RATIO="${missing_package_ratio}" \
"${PY_JSON}" - <<'PY'
import json
import os

log_path = os.environ.get("PYRIGHT_LOG_PATH", "")
try:
    tail = "\n".join(
        open(log_path, "r", encoding="utf-8", errors="replace").read().splitlines()[-240:]
    ).strip()
except Exception:
    tail = ""

payload = {
    "status": os.environ.get("PYRIGHT_STATUS", "failure"),
    "skip_reason": os.environ.get("PYRIGHT_SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "1")),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYRIGHT_PYTHON", ""),
        "git_commit": os.environ.get("PYRIGHT_GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "pyright_install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0"))),
        "pyright_install_command": os.environ.get("PYRIGHT_INSTALL_CMD", ""),
        "python_resolution_warning": os.environ.get("PYRIGHT_PYWARN", ""),
        "decision_reason": os.environ.get("PYRIGHT_DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("PYRIGHT_FAILURE_CATEGORY", ""),
    "error_excerpt": tail,
    "missing_packages_count": int(os.environ.get("PYRIGHT_MISSING_COUNT", "0")),
    "total_imported_packages_count": int(os.environ.get("PYRIGHT_IMPORTED_COUNT", "0")),
    "missing_package_ratio": os.environ.get("PYRIGHT_MISSING_RATIO", ""),
}

out_path = os.environ.get("PYRIGHT_RESULTS_JSON", "")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
PY

wrote_results=1
exit 0
