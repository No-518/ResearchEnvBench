#!/usr/bin/env bash
set -u
set -o pipefail

STAGE="pyright"
TASK="check"
FRAMEWORK="unknown"
TIMEOUT_SEC="${SCIMLOPSBENCH_PYRIGHT_TIMEOUT_SEC:-600}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR_DEFAULT="${REPO_ROOT}/build_output/${STAGE}"

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (always written):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection:
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use report python_path if available, else python from PATH

Other:
  --repo <path>                  Repository root (default: current repo root)
  --level <error|warning|...>    Default: error
  --out-dir <path>               Default: build_output/pyright
  -- <pyright args...>           Extra args passed to Pyright
EOF
}

mode="system"
repo="${REPO_ROOT}"
out_dir="${OUT_DIR_DEFAULT}"
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

mkdir -p "${out_dir}"
LOG_PATH="${out_dir}/log.txt"
PYRIGHT_OUT_JSON="${out_dir}/pyright_output.json"
ANALYSIS_JSON="${out_dir}/analysis.json"
RESULTS_JSON="${out_dir}/results.json"

TS_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || true)"

# Initialize mandatory outputs to avoid stale artifacts on early termination.
echo "{}" > "${PYRIGHT_OUT_JSON}"
echo "{}" > "${ANALYSIS_JSON}"
cat > "${RESULTS_JSON}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "benchmark_scripts/run_pyright_missing_imports.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "pyright stage placeholder (not completed)",
    "timestamp_utc": "${TS_UTC}",
    "placeholder": true
  },
  "failure_category": "unknown",
  "error_excerpt": "stage did not complete"
}
EOF

: > "${LOG_PATH}"
exec > >(tee -a "${LOG_PATH}") 2>&1

PY_SYS="$(command -v python3 || command -v python || true)"
if [[ -z "${PY_SYS}" ]]; then
  echo "[pyright] No python found in PATH." >&2
fi

cd "${repo}"

py_cmd=()
python_source=""
python_warning=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

if [[ -n "${python_bin}" ]]; then
  py_cmd=("${python_bin}")
  python_source="cli"
else
  case "${mode}" in
    venv)
      if [[ -z "${venv_dir}" ]]; then echo "[pyright] --venv required for --mode venv" >&2; exit 2; fi
      py_cmd=("${venv_dir}/bin/python")
      python_source="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("${venv_dir}/bin/python")
      python_source="uv"
      ;;
    conda)
      if [[ -z "${conda_env}" ]]; then echo "[pyright] --conda-env required for --mode conda" >&2; exit 2; fi
      command -v conda >/dev/null 2>&1 || { echo "[pyright] conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "${conda_env}" python)
      python_source="conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "[pyright] poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_source="poetry"
      ;;
    system)
      # Prefer benchmarked python_path from report.json when available.
      if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
        py_cmd=("${SCIMLOPSBENCH_PYTHON}")
        python_source="env:SCIMLOPSBENCH_PYTHON"
      else
        if [[ -z "${PY_SYS}" ]]; then
          echo "[pyright] python not found in PATH; cannot resolve report python_path." >&2
          cat > "${RESULTS_JSON}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "benchmark_scripts/run_pyright_missing_imports.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python is required to resolve python_path from report.json",
    "timestamp_utc": "${TS_UTC}"
  },
  "failure_category": "deps",
  "error_excerpt": "python not found in PATH"
}
EOF
          exit 1
        fi

        if [[ ! -f "${report_path}" ]]; then
          echo "[pyright] Missing report.json: ${report_path}" >&2
          cat > "${RESULTS_JSON}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "benchmark_scripts/run_pyright_missing_imports.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {"SCIMLOPSBENCH_REPORT": "${SCIMLOPSBENCH_REPORT:-}"},
    "decision_reason": "pyright stage requires python_path from the agent report.json",
    "timestamp_utc": "${TS_UTC}"
  },
  "failure_category": "missing_report",
  "error_excerpt": "missing report.json"
}
EOF
          exit 1
        fi

        report_payload="$("${PY_SYS}" - <<'PY' "${report_path}" 2>/dev/null || true
import json, sys
p = sys.argv[1]
try:
  d = json.load(open(p, "r", encoding="utf-8"))
  print("OK")
  print(str(d.get("python_path") or ""))
except Exception as e:
  print("ERR")
  print(repr(e))
PY
)"
        report_ok="$(printf "%s\n" "${report_payload}" | sed -n '1p' || true)"
        report_py="$(printf "%s\n" "${report_payload}" | sed -n '2p' || true)"

        if [[ "${report_ok}" != "OK" ]]; then
          echo "[pyright] Invalid report.json: ${report_path}" >&2
          cat > "${RESULTS_JSON}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "benchmark_scripts/run_pyright_missing_imports.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {"SCIMLOPSBENCH_REPORT": "${SCIMLOPSBENCH_REPORT:-}"},
    "decision_reason": "pyright stage requires a valid report.json with python_path",
    "timestamp_utc": "${TS_UTC}"
  },
  "failure_category": "missing_report",
  "error_excerpt": "invalid report.json"
}
EOF
          exit 1
        fi

        if [[ -n "${report_py}" ]]; then
          py_cmd=("${report_py}")
          python_source="report:python_path"
        else
          py_cmd=("${PY_SYS}")
          python_source="path_fallback"
          python_warning="python_path missing in report.json; using python from PATH as last resort"
          echo "[pyright] WARNING: ${python_warning}" >&2
        fi
      fi
      ;;
    *)
      echo "[pyright] Unknown --mode: ${mode}" >&2
      usage
      exit 2
      ;;
  esac
fi

if [[ ${#py_cmd[@]} -eq 0 ]]; then
  echo "[pyright] Failed to resolve python command." >&2
  exit 1
fi

if ! "${py_cmd[@]}" -c "import sys; print(sys.executable)" >/dev/null 2>&1; then
  echo "[pyright] Failed to run python via: ${py_cmd[*]}" >&2
  echo "{}" > "${PYRIGHT_OUT_JSON}"
  echo "{}" > "${ANALYSIS_JSON}"
  "${PY_SYS:-python3}" - <<'PY' "${RESULTS_JSON}" "${TIMEOUT_SEC}" "${python_source}"
import json, sys, time, os
results_path = sys.argv[1]
timeout_sec = int(sys.argv[2])
python_source = sys.argv[3]
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "Failed to invoke selected python interpreter for pyright.",
    "python_source": python_source,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": "deps",
  "error_excerpt": "failed to invoke selected python interpreter",
}
os.makedirs(os.path.dirname(results_path), exist_ok=True)
with open(results_path, "w", encoding="utf-8") as f:
  json.dump(payload, f, ensure_ascii=False, indent=2); f.write("\\n")
PY
  exit 1
fi

install_attempted=0
install_cmd=""
install_ok=0

if ! "${py_cmd[@]}" -c "import pyright" >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] Installing pyright: ${install_cmd}"
  if "${py_cmd[@]}" -m pip install -q pyright; then
    install_ok=1
  else
    install_ok=0
  fi
fi

project_args=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
elif [[ -f "pyproject.toml" ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
elif [[ -d "src" ]]; then
  targets+=(src)
  [[ -d "tests" ]] && targets+=(tests)
else
  # Detect top-level packages under subprojects that have setup.py/pyproject.toml.
  while IFS= read -r init_file; do
    pkg_dir="${init_file%/__init__.py}"
    targets+=("${pkg_dir#./}")
  done < <(
    find . -maxdepth 2 -type f -name 'setup.py' -printf '%h\n' 2>/dev/null \
      | sort -u \
      | while IFS= read -r proj; do
          find "$proj" -maxdepth 2 -type f -name '__init__.py' -print 2>/dev/null || true
        done
  )
fi

targets=($(printf "%s\n" "${targets[@]}" | sort -u))

if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  echo "[pyright] No pyrightconfig/pyproject/src/package targets found; failing." >&2
  echo "{}" > "${PYRIGHT_OUT_JSON}"
  echo "{}" > "${ANALYSIS_JSON}"
  "${PY_SYS:-python3}" - <<'PY' "${RESULTS_JSON}" "${TIMEOUT_SEC}"
import json, sys, time, os
results_path = sys.argv[1]
timeout_sec = int(sys.argv[2])
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "missing_packages_count": 0,
  "total_imported_packages_count": 0,
  "missing_package_ratio": "0/0",
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "No Python package roots detected for Pyright target selection.",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": "entrypoint_not_found",
  "error_excerpt": "no pyright targets",
}
os.makedirs(os.path.dirname(results_path), exist_ok=True)
with open(results_path, "w", encoding="utf-8") as f:
  json.dump(payload, f, ensure_ascii=False, indent=2); f.write("\\n")
PY
  exit 1
fi

command_str=("${py_cmd[@]}" -m pyright --level "${pyright_level}" --outputjson "${project_args[@]}" "${pyright_extra_args[@]}")
if [[ ${#project_args[@]} -eq 0 ]]; then
  command_str+=("${targets[@]}")
fi

echo "[pyright] Running: ${command_str[*]}"
pyright_rc=0
if "${command_str[@]}" > "${PYRIGHT_OUT_JSON}"; then
  pyright_rc=0
else
  pyright_rc=$?
  echo "[pyright] pyright exited non-zero (${pyright_rc}); continuing to parse JSON."
fi

if [[ ! -s "${PYRIGHT_OUT_JSON}" ]]; then
  echo "{}" > "${PYRIGHT_OUT_JSON}"
fi

PY_ANALYZE="$("${py_cmd[@]}" - <<'PY' "${PYRIGHT_OUT_JSON}" "${ANALYSIS_JSON}" "${RESULTS_JSON}" "${TIMEOUT_SEC}" "${python_source}" "${install_attempted}" "${install_cmd}" "${install_ok}" "${pyright_rc}" "${pyright_level}" "${python_warning}" "${py_cmd[*]}" "${command_str[*]}"
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Iterable

out_json = pathlib.Path(sys.argv[1])
analysis_json = pathlib.Path(sys.argv[2])
results_json = pathlib.Path(sys.argv[3])
timeout_sec = int(sys.argv[4])
python_source = sys.argv[5]
install_attempted = bool(int(sys.argv[6]))
install_cmd = sys.argv[7]
install_ok = bool(int(sys.argv[8]))
pyright_rc = int(sys.argv[9])
pyright_level = sys.argv[10]
python_warning = sys.argv[11]
python_cmd = sys.argv[12] if len(sys.argv) > 12 else ""
pyright_command = sys.argv[13] if len(sys.argv) > 13 else ""

repo_root = pathlib.Path(".").resolve()

def base_assets():
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

def safe_load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""

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
        "benchmark_scripts",
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set[str]:
    pkgs: set[str] = set()
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

all_imported_packages: set[str] = set()
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
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python_cmd": python_cmd,
        "python_source": python_source,
        "python_warning": python_warning,
        "pyright_level": pyright_level,
        "pyright_returncode": pyright_rc,
        "files_scanned": files_scanned,
        "pyright_install_attempted": install_attempted,
        "pyright_install_command": install_cmd,
        "pyright_install_ok": install_ok,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "success" if install_ok or not install_attempted else "failure",
    "skip_reason": "not_applicable",
    "exit_code": 0 if (install_ok or not install_attempted) else 1,
    "stage": "pyright",
    "task": "check",
    "command": pyright_command,
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": base_assets(),
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "meta": {
        "python": sys.version.split()[0],
        "git_commit": git_commit(),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": "Run Pyright on detected package roots and count only reportMissingImports diagnostics.",
        "pyright_install_attempted": install_attempted,
        "pyright_install_command": install_cmd,
        "pyright_install_ok": install_ok,
        "python_source": python_source,
        "python_warning": python_warning,
        "timestamp_utc": analysis_payload["meta"]["timestamp_utc"],
    },
    "failure_category": "" if (install_ok or not install_attempted) else "deps",
    "error_excerpt": "",
}
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(results_payload["missing_package_ratio"])
PY
)"

if [[ ${install_attempted} -eq 1 && ${install_ok} -ne 1 ]]; then
  # Installation failed: results.json already written with failure.
  exit 1
fi

exit 0
