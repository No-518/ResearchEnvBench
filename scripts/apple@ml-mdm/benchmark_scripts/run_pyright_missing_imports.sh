#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule=reportMissingImports).

Defaults:
  - Repo: repository root (auto-detected from this script location)
  - Python: resolved from agent report.json (unless --python or --mode is provided)

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Python selection (highest to lowest):
  1) --python <path>
  2) --mode venv|uv|conda|poetry|system (explicit)
  3) SCIMLOPSBENCH_PYTHON env var
  4) python_path from report.json (SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)

Options:
  --repo <path>                  Repo root to analyze (default: auto)
  --out-dir <path>               Output dir (default: build_output/pyright)
  --level <error|warning|...>    Pyright diagnostic level (default: error)
  --python <path>                Explicit python executable to use (highest priority)
  --report-path <path>           Override report.json path (default: /opt/scimlopsbench/report.json)
  --mode <venv|uv|conda|poetry|system>
  --venv <path>                  For --mode venv|uv (default for uv: .venv)
  --conda-env <name>             For --mode conda
  --                              Extra args passed to pyright

Examples:
  bash benchmark_scripts/run_pyright_missing_imports.sh
  bash benchmark_scripts/run_pyright_missing_imports.sh --python /opt/scimlopsbench/python
  bash benchmark_scripts/run_pyright_missing_imports.sh --mode venv --venv .venv
  bash benchmark_scripts/run_pyright_missing_imports.sh -- --verifytypes ml_mdm
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

repo="${repo_root}"
out_dir="${repo_root}/build_output/pyright"
pyright_level="error"

python_bin=""
report_path=""
mode=""
venv_dir=""
conda_env=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
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

mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

cd "$repo"

git_commit=""
if command -v git >/dev/null 2>&1; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

resolve_python_via_report() {
  local rp="${1:-}"
  python3 "${repo_root}/benchmark_scripts/runner.py" resolve-python ${rp:+--report-path "$rp"}
}

py_cmd=()
python_resolution_source=""
python_resolution_warning=""
python_version=""
pyright_install_attempted=0
pyright_install_cmd=""
pyright_install_rc=0

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution_source="cli"
elif [[ -n "$mode" ]]; then
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      python_resolution_source="mode:venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution_source="mode:uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_resolution_source="mode:conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_resolution_source="mode:poetry"
      ;;
    system)
      py_cmd=(python3)
      python_resolution_source="mode:system"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
else
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    py_cmd=("${SCIMLOPSBENCH_PYTHON}")
    python_resolution_source="env:SCIMLOPSBENCH_PYTHON"
  else
    resolved="$(resolve_python_via_report "$report_path" || true)"
    if [[ -z "$resolved" ]]; then
      echo "Failed to resolve python via report.json; set --python or SCIMLOPSBENCH_PYTHON or provide a valid report.json." >&2
      python_resolution_warning="missing_report"
      # fall through: mark failure below
    else
      py_cmd=("$resolved")
      python_resolution_source="report.json"
    fi
  fi
fi

write_failure_results() {
  local failure_category="${1:-unknown}"
  local message="${2:-}"
  printf '%s\n' "${message}" >> "$log_path"
  # Ensure required JSON artifacts exist.
  if [[ ! -f "$out_json" ]]; then
    printf '%s\n' '{"error":"pyright did not run"}' > "$out_json"
  fi
  PYRIGHT_OUT_DIR="$out_dir" PYRIGHT_OUT_JSON="$out_json" PYRIGHT_ANALYSIS_JSON="$analysis_json" PYRIGHT_RESULTS_JSON="$results_json" \
  PYRIGHT_FAILURE_CATEGORY="$failure_category" python3 - <<'PY'
import json
import os
import pathlib

out_dir = pathlib.Path(os.environ["PYRIGHT_OUT_DIR"])
analysis_json = pathlib.Path(os.environ["PYRIGHT_ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["PYRIGHT_RESULTS_JSON"])

def tail(path: pathlib.Path, n=220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

assets = {
  "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
  "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

analysis_payload = {
  "missing_packages": [],
  "pyright": {},
  "meta": {
    "mode": os.environ.get("PYRIGHT_MODE", ""),
    "python_cmd": os.environ.get("PYRIGHT_PY_CMD", ""),
    "python_resolution_source": os.environ.get("PYRIGHT_PY_SOURCE", ""),
    "python_resolution_warning": os.environ.get("PYRIGHT_PY_WARNING", ""),
    "pyright_install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED","0") or "0")),
    "pyright_install_cmd": os.environ.get("PYRIGHT_INSTALL_CMD",""),
    "pyright_install_rc": int(os.environ.get("PYRIGHT_INSTALL_RC","0") or "0"),
    "targets": [],
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
  },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": os.environ.get("PYRIGHT_CMD",""),
  "timeout_sec": int(os.environ.get("PYRIGHT_TIMEOUT_SEC","600") or "600"),
  "framework": "unknown",
  "assets": assets,
  "meta": {
    "python": os.environ.get("PYRIGHT_PY_VERSION",""),
    "git_commit": os.environ.get("PYRIGHT_GIT_COMMIT",""),
    "env_vars": {k: os.environ.get(k,"") for k in [
      "SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","PIP_CACHE_DIR","XDG_CACHE_HOME"
    ] if os.environ.get(k)},
  "decision_reason": os.environ.get("PYRIGHT_DECISION_REASON",""),
  "pyright_install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED","0") or "0")),
  "pyright_install_cmd": os.environ.get("PYRIGHT_INSTALL_CMD",""),
  "pyright_install_rc": int(os.environ.get("PYRIGHT_INSTALL_RC","0") or "0"),
  "python_resolution_source": os.environ.get("PYRIGHT_PY_SOURCE",""),
  "python_resolution_warning": os.environ.get("PYRIGHT_PY_WARNING",""),
  },
  "failure_category": os.environ.get("PYRIGHT_FAILURE_CATEGORY", "unknown"),
  "error_excerpt": tail(out_dir / "log.txt"),
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "missing_packages": [],
}
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

if [[ ${#py_cmd[@]} -eq 0 ]]; then
  PYRIGHT_PY_WARNING="${python_resolution_warning}" PYRIGHT_PY_SOURCE="${python_resolution_source}" \
    PYRIGHT_GIT_COMMIT="${git_commit}" PYRIGHT_DECISION_REASON="python resolution failed" \
    write_failure_results "missing_report" "pyright stage failure: could not resolve python"
  exit 1
fi

echo "Using python: ${py_cmd[*]}"
if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  PYRIGHT_PY_CMD="${py_cmd[*]}" PYRIGHT_PY_SOURCE="${python_resolution_source}" \
    PYRIGHT_GIT_COMMIT="${git_commit}" PYRIGHT_DECISION_REASON="python command failed to execute" \
    write_failure_results "missing_report" "pyright stage failure: unable to execute python: ${py_cmd[*]}"
  exit 1
fi

python_version="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

targets=()
project_arg=()
decision_reason=""

if [[ -f "pyrightconfig.json" ]]; then
  project_arg=(--project pyrightconfig.json)
  decision_reason="Using pyrightconfig.json"
elif [[ -f "pyproject.toml" ]] && { command -v rg >/dev/null 2>&1 && rg -n "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" -S pyproject.toml >/dev/null 2>&1 || grep -Eq "^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$" pyproject.toml; }; then
  project_arg=(--project pyproject.toml)
  decision_reason="Using pyproject.toml [tool.pyright]"
elif [[ -d "src" ]]; then
  targets+=(src)
  [[ -d "tests" ]] && targets+=(tests)
  decision_reason="Using src/ layout targets"
else
  # Heuristic for mono-repos: include pyproject roots (shallow) and python package dirs.
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    targets+=("$p")
  done < <(find . -maxdepth 2 -name pyproject.toml -not -path "./benchmark_*/*" -not -path "./build_output/*" -print \
    | sed 's|^\\./||' | xargs -r -n1 dirname | sort -u)

  if [[ ${#targets[@]} -gt 0 ]]; then
    decision_reason="Using pyproject.toml project roots as targets"
  else
    while IFS= read -r p; do
      [[ -z "$p" ]] && continue
      targets+=("$p")
    done < <(find . -maxdepth 4 -name __init__.py -not -path "./benchmark_*/*" -not -path "./build_output/*" -print \
      | sed 's|^\\./||' | xargs -r -n1 dirname | sort -u)
    decision_reason="Using detected __init__.py package dirs as targets"
  fi
fi

if [[ ${#project_arg[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  PYRIGHT_PY_CMD="${py_cmd[*]}" PYRIGHT_PY_SOURCE="${python_resolution_source}" \
    PYRIGHT_PY_WARNING="${python_resolution_warning}" PYRIGHT_PY_VERSION="${python_version}" \
    PYRIGHT_GIT_COMMIT="${git_commit}" PYRIGHT_DECISION_REASON="no pyrightconfig/pyproject/src/package dirs found" \
    write_failure_results "entrypoint_not_found" "pyright stage failure: could not determine analysis targets"
  exit 1
fi

echo "Pyright targets: ${targets[*]:-<project config>}"
echo "Pyright project args: ${project_arg[*]:-<none>}"

PIP_CACHE_DIR="${repo_root}/benchmark_assets/cache/pip" XDG_CACHE_HOME="${repo_root}/benchmark_assets/cache/xdg" \
  mkdir -p "${repo_root}/benchmark_assets/cache/pip" "${repo_root}/benchmark_assets/cache/xdg" || true

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  pyright_install_attempted=1
  pyright_install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "Installing pyright into selected python: $pyright_install_cmd"
  if ! "${py_cmd[@]}" -m pip --version >/dev/null 2>&1; then
    pyright_install_rc=127
    PYRIGHT_PY_CMD="${py_cmd[*]}" PYRIGHT_PY_SOURCE="${python_resolution_source}" \
      PYRIGHT_PY_VERSION="${python_version}" PYRIGHT_INSTALL_ATTEMPTED="${pyright_install_attempted}" \
      PYRIGHT_INSTALL_CMD="${pyright_install_cmd}" PYRIGHT_INSTALL_RC="${pyright_install_rc}" \
      PYRIGHT_GIT_COMMIT="${git_commit}" PYRIGHT_DECISION_REASON="pip is not available in the selected python environment" \
      write_failure_results "deps" "pyright stage failure: pip is not available in the selected python environment"
    exit 1
  fi
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pyright_install_rc=$?
  set -e
  if [[ $pyright_install_rc -ne 0 ]]; then
    PYRIGHT_PY_CMD="${py_cmd[*]}" PYRIGHT_PY_SOURCE="${python_resolution_source}" \
      PYRIGHT_PY_VERSION="${python_version}" PYRIGHT_INSTALL_ATTEMPTED="${pyright_install_attempted}" \
      PYRIGHT_INSTALL_CMD="${pyright_install_cmd}" PYRIGHT_INSTALL_RC="${pyright_install_rc}" \
      PYRIGHT_GIT_COMMIT="${git_commit}" PYRIGHT_DECISION_REASON="pyright install failed" \
      write_failure_results "download_failed" "pyright stage failure: pyright install failed (rc=$pyright_install_rc)"
    exit 1
  fi
fi

pyright_cmd=("${py_cmd[@]}" -m pyright "${project_arg[@]}" "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}")
echo "Running: ${pyright_cmd[*]}"

set +e
"${pyright_cmd[@]}" >"$out_json"
pyright_rc=$?
set -e
echo "Pyright exit code (ignored for stage success): $pyright_rc"

PYRIGHT_MODE="${mode}" PYRIGHT_PY_CMD="${py_cmd[*]}" PYRIGHT_PY_SOURCE="${python_resolution_source}" \
PYRIGHT_PY_WARNING="${python_resolution_warning}" PYRIGHT_PY_VERSION="${py_version:-$python_version}" \
PYRIGHT_INSTALL_ATTEMPTED="${pyright_install_attempted}" PYRIGHT_INSTALL_CMD="${pyright_install_cmd}" \
PYRIGHT_INSTALL_RC="${pyright_install_rc}" PYRIGHT_GIT_COMMIT="${git_commit}" \
PYRIGHT_DECISION_REASON="${decision_reason}" PYRIGHT_CMD="${pyright_cmd[*]}" PYRIGHT_TIMEOUT_SEC="600" \
PYRIGHT_TARGETS="${targets[*]}" PYRIGHT_OUT_JSON="${out_json}" PYRIGHT_ANALYSIS_JSON="${analysis_json}" PYRIGHT_RESULTS_JSON="${results_json}" \
"${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable, List, Set

repo_root = pathlib.Path(".").resolve()
out_json = pathlib.Path(os.environ.get("PYRIGHT_OUT_JSON", "build_output/pyright/pyright_output.json"))
analysis_json = pathlib.Path(os.environ.get("PYRIGHT_ANALYSIS_JSON", "build_output/pyright/analysis.json"))
results_json = pathlib.Path(os.environ.get("PYRIGHT_RESULTS_JSON", "build_output/pyright/results.json"))

def safe_load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

data = safe_load_json(out_json)
diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

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

targets: List[str] = (os.environ.get("PYRIGHT_TARGETS") or "").split()

def iter_py_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> Set[str]:
    pkgs: Set[str] = set()
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

files_scanned = 0
all_imported_packages: Set[str] = set()

scan_roots: List[pathlib.Path] = []
if targets:
    for t in targets:
        p = (repo_root / t).resolve()
        if p.exists():
            scan_roots.append(p)
else:
    scan_roots = [repo_root]

for root in scan_roots:
    for py_file in iter_py_files(root):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("PYRIGHT_MODE", ""),
        "python_cmd": os.environ.get("PYRIGHT_PY_CMD", ""),
        "python_resolution_source": os.environ.get("PYRIGHT_PY_SOURCE", ""),
        "python_resolution_warning": os.environ.get("PYRIGHT_PY_WARNING", ""),
        "pyright_install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0") or "0")),
        "pyright_install_cmd": os.environ.get("PYRIGHT_INSTALL_CMD", ""),
        "pyright_install_rc": int(os.environ.get("PYRIGHT_INSTALL_RC", "0") or "0"),
        "files_scanned": files_scanned,
        "scan_roots": [str(p) for p in scan_roots],
        "targets": targets,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.parent.mkdir(parents=True, exist_ok=True)
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "success",
    "skip_reason": "not_applicable",
    "exit_code": 0,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": int(os.environ.get("PYRIGHT_TIMEOUT_SEC", "600") or "600"),
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYRIGHT_PY_VERSION", ""),
        "git_commit": os.environ.get("PYRIGHT_GIT_COMMIT", ""),
        "env_vars": {k: os.environ.get(k,"") for k in [
            "SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","PIP_CACHE_DIR","XDG_CACHE_HOME"
        ] if os.environ.get(k)},
        "decision_reason": os.environ.get("PYRIGHT_DECISION_REASON", ""),
        "pyright_install_attempted": bool(int(os.environ.get("PYRIGHT_INSTALL_ATTEMPTED", "0") or "0")),
        "pyright_install_cmd": os.environ.get("PYRIGHT_INSTALL_CMD", ""),
        "pyright_install_rc": int(os.environ.get("PYRIGHT_INSTALL_RC", "0") or "0"),
        "python_resolution_source": os.environ.get("PYRIGHT_PY_SOURCE", ""),
        "python_resolution_warning": os.environ.get("PYRIGHT_PY_WARNING", ""),
    },
    "failure_category": "unknown",
    "error_excerpt": "",
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "missing_packages": missing_packages,
}
results_json.parent.mkdir(parents=True, exist_ok=True)
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

exit 0
