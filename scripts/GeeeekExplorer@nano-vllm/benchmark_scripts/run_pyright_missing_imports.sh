#!/usr/bin/env bash
set -euo pipefail

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
  --mode system                  Use: python from PATH (explicit override)

Optional:
  --report-path <path>           Default: /opt/scimlopsbench/report.json (or $SCIMLOPSBENCH_REPORT)
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright
EOF
}

mode="report"
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

REPO_ROOT="$(cd "$repo" && pwd)"
OUT_DIR_ABS="$REPO_ROOT/$out_dir"
mkdir -p "$OUT_DIR_ABS"

LOG_TXT="$OUT_DIR_ABS/log.txt"
OUT_JSON="$OUT_DIR_ABS/pyright_output.json"
ANALYSIS_JSON="$OUT_DIR_ABS/analysis.json"
RESULTS_JSON="$OUT_DIR_ABS/results.json"

exec > >(tee "$LOG_TXT") 2>&1

PY_BOOTSTRAP=""
if command -v python3 >/dev/null 2>&1; then
  PY_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BOOTSTRAP="python"
else
  echo "ERROR: python/python3 not found in PATH" >&2
  exit 1
fi

timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ" || true)"
git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"

fail_with_json() {
  local failure_category="$1"
  local message="$2"
  local cmd_string="${3:-}"
  local exit_code="${4:-1}"

  "$PY_BOOTSTRAP" - <<PY || true
import json, pathlib
repo_root = pathlib.Path(${REPO_ROOT@Q})
out_json = pathlib.Path(${OUT_JSON@Q})
analysis_json = pathlib.Path(${ANALYSIS_JSON@Q})
results_json = pathlib.Path(${RESULTS_JSON@Q})

def tail(p: pathlib.Path, n: int = 240) -> str:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

out_json.write_text("{}", encoding="utf-8")
analysis_json.write_text(json.dumps({
  "missing_packages": [],
  "pyright": None,
  "meta": {
    "repo_root": str(repo_root),
    "pyright_targets": [],
    "python_cmd": "",
    "install_attempted": False,
    "install_command": "",
    "timestamp_utc": ${timestamp_utc@Q},
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
  },
}, indent=2), encoding="utf-8")

results_json.write_text(json.dumps({
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": ${cmd_string@Q},
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
  },
  "meta": {
    "python": "",
    "git_commit": ${git_commit@Q},
    "env_vars": {},
    "decision_reason": ${message@Q},
    "timestamp_utc": ${timestamp_utc@Q},
  },
  "failure_category": ${failure_category@Q},
  "error_excerpt": tail(pathlib.Path(${LOG_TXT@Q}), 240),
}, indent=2), encoding="utf-8")
PY
  exit "$exit_code"
}

resolve_report_path() {
  if [[ -n "$report_path" ]]; then
    echo "$report_path"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_REPORT:-}" ]]; then
    echo "$SCIMLOPSBENCH_REPORT"
    return 0
  fi
  echo "/opt/scimlopsbench/report.json"
}

resolve_python_from_report() {
  local rp="$1"
  if [[ ! -f "$rp" ]]; then
    return 1
  fi
  "$PY_BOOTSTRAP" - <<PY
import json, pathlib, sys
p = pathlib.Path(${rp@Q})
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(1)
pp = data.get("python_path")
if not isinstance(pp, str) or not pp.strip():
  sys.exit(1)
print(pp)
PY
}

py_cmd=()
python_resolution="unknown"
python_warning=""

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_resolution="cli"
elif [[ "$mode" != "report" ]]; then
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        fail_with_json "args_unknown" "--venv is required for --mode venv" "" 1
      fi
      py_cmd=("$venv_dir/bin/python")
      python_resolution="mode:venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_resolution="mode:uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        fail_with_json "args_unknown" "--conda-env is required for --mode conda" "" 1
      fi
      command -v conda >/dev/null 2>&1 || fail_with_json "entrypoint_not_found" "conda not found in PATH" "" 1
      py_cmd=(conda run -n "$conda_env" python)
      python_resolution="mode:conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || fail_with_json "entrypoint_not_found" "poetry not found in PATH" "" 1
      py_cmd=(poetry run python)
      python_resolution="mode:poetry"
      ;;
    system)
      py_cmd=(python)
      python_resolution="mode:system"
      python_warning="Using python from PATH due to --mode system."
      ;;
    *)
      fail_with_json "args_unknown" "Unknown --mode: $mode" "" 1
      ;;
  esac
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("$SCIMLOPSBENCH_PYTHON")
  python_resolution="env:SCIMLOPSBENCH_PYTHON"
else
  rp="$(resolve_report_path)"
  if python_from_report="$(resolve_python_from_report "$rp" 2>/dev/null)"; then
    py_cmd=("$python_from_report")
    python_resolution="report:python_path"
  else
    fail_with_json "missing_report" "Missing/invalid report.json (provide --python/--mode or set SCIMLOPSBENCH_PYTHON/SCIMLOPSBENCH_REPORT)." "" 1
  fi
fi

if [[ -z "${py_cmd[*]:-}" ]]; then
  fail_with_json "missing_report" "Failed to resolve python interpreter." "" 1
fi

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  fail_with_json "path_hallucination" "Failed to run python via resolved command: ${py_cmd[*]}" "${py_cmd[*]}" 1
fi

cd "$REPO_ROOT"

# Determine how to invoke Pyright.
pyright_args=()
decision_reason=""
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  pyright_args+=(--project "pyrightconfig.json")
  decision_reason="Using pyrightconfig.json (--project pyrightconfig.json)."
elif [[ -f "pyproject.toml" ]] && grep -q '^\[tool\.pyright\]' pyproject.toml 2>/dev/null; then
  pyright_args+=(--project "pyproject.toml")
  decision_reason="Using pyproject.toml [tool.pyright] (--project pyproject.toml)."
elif [[ -d "src" ]]; then
  targets+=("src")
  if [[ -d "tests" ]]; then
    targets+=("tests")
  fi
  decision_reason="Using src/ layout targets."
else
  mapfile -t init_files < <(find . -name "__init__.py" \
    -not -path "./.git/*" \
    -not -path "./.venv/*" \
    -not -path "./venv/*" \
    -not -path "./build/*" \
    -not -path "./dist/*" \
    -not -path "./node_modules/*" \
    -not -path "./build_output/*" \
    -not -path "./benchmark_assets/*" \
    -not -path "./benchmark_scripts/*" 2>/dev/null || true)
  if [[ "${#init_files[@]}" -gt 0 ]]; then
    declare -A seen=()
    for f in "${init_files[@]}"; do
      d="$(dirname "$f")"
      d="${d#./}"
      if [[ -n "$d" && -z "${seen[$d]:-}" ]]; then
        targets+=("$d")
        seen["$d"]=1
      fi
    done
    decision_reason="Detected Python packages via __init__.py."
  fi
fi

if [[ "${#pyright_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  fail_with_json "entrypoint_not_found" "No Pyright project config, src/, or package dirs detected; nothing to analyze." "" 1
fi

install_attempted=0
install_command=""
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_command="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] pyright not importable; attempting install: $install_command"
  set +e
  pip_out="$("${py_cmd[@]}" -m pip install -q pyright 2>&1)"
  pip_rc=$?
  set -e
  echo "$pip_out"
  if [[ "$pip_rc" -ne 0 ]]; then
    if echo "$pip_out" | grep -Ei 'temporary failure|name or service not known|connection|timed out|readtimeout|proxy' >/dev/null 2>&1; then
      fail_with_json "download_failed" "Failed to install pyright (likely offline/network). Command: $install_command" "$install_command" 1
    fi
    fail_with_json "deps" "Failed to install pyright via pip (rc=$pip_rc). Command: $install_command" "$install_command" 1
  fi
fi

pyright_cmd=("${py_cmd[@]}" -m pyright)
if [[ "${#targets[@]}" -gt 0 ]]; then
  pyright_cmd+=("${targets[@]}")
fi
pyright_cmd+=(--level "$pyright_level" --outputjson)
pyright_cmd+=("${pyright_args[@]}")
if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
  pyright_cmd+=("${pyright_extra_args[@]}")
fi

echo "[pyright] command: ${pyright_cmd[*]}"

: >"$OUT_JSON"
set +e
"${pyright_cmd[@]}" >"$OUT_JSON"
pyright_rc=$?
set -e
echo "[pyright] exit_code=$pyright_rc (non-zero does not fail this stage by itself)"

PY_CMD_STR="${py_cmd[*]}"
PYRIGHT_CMD_STR="${pyright_cmd[*]}"
MODE_STR="$mode"
DECISION_REASON="$decision_reason"
INSTALL_ATTEMPTED="$install_attempted"
INSTALL_COMMAND="$install_command"
PYTHON_RESOLUTION="$python_resolution"
PYTHON_WARNING="$python_warning"
OUT_JSON="$OUT_JSON" ANALYSIS_JSON="$ANALYSIS_JSON" RESULTS_JSON="$RESULTS_JSON" LOG_TXT="$LOG_TXT" REPO_ROOT="$REPO_ROOT" \
  PY_CMD_STR="$PY_CMD_STR" PYRIGHT_CMD_STR="$PYRIGHT_CMD_STR" MODE_STR="$MODE_STR" DECISION_REASON="$DECISION_REASON" INSTALL_ATTEMPTED="$INSTALL_ATTEMPTED" INSTALL_COMMAND="$INSTALL_COMMAND" \
  PYRIGHT_RC="$pyright_rc" PYTHON_RESOLUTION="$PYTHON_RESOLUTION" PYTHON_WARNING="$PYTHON_WARNING" GIT_COMMIT="$git_commit" TIMESTAMP_UTC="$timestamp_utc" \
  "${py_cmd[@]}" - <<'PY'
import ast
import hashlib
import json
import os
import pathlib
import re
from typing import Iterable

repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_txt = pathlib.Path(os.environ["LOG_TXT"])

pyright_rc = int(os.environ.get("PYRIGHT_RC", "0"))

def tail(p: pathlib.Path, n: int = 240) -> str:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

raw = None
parse_error = ""
try:
    raw = json.loads(out_json.read_text(encoding="utf-8"))
except Exception as e:
    parse_error = f"{type(e).__name__}: {e}"
    raw = None

missing_diags = []
if isinstance(raw, dict):
    diagnostics = raw.get("generalDiagnostics", []) or []
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
        "repo_root": str(repo_root),
        "mode": os.environ.get("MODE_STR", ""),
        "python_cmd": os.environ.get("PY_CMD_STR", ""),
        "python_resolution": os.environ.get("PYTHON_RESOLUTION", ""),
        "python_warning": os.environ.get("PYTHON_WARNING", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_exit_code": pyright_rc,
        "pyright_output_sha256": sha256_file(out_json) if out_json.exists() else "",
        "files_scanned": files_scanned,
        "timestamp_utc": os.environ.get("TIMESTAMP_UTC", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
    "errors": {
        "pyright_output_parse_error": parse_error,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

status = "success"
failure_category = ""
exit_code = 0
error_excerpt = ""

if parse_error:
    status = "failure"
    failure_category = "invalid_json"
    exit_code = 1
    error_excerpt = tail(log_txt, 240)

results_payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "metrics": analysis_payload["metrics"],
    "missing_packages": missing_packages,
    "meta": {
        "python": os.environ.get("PY_CMD_STR", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {},
        "decision_reason": analysis_payload["meta"]["decision_reason"],
        "install_attempted": analysis_payload["meta"]["install_attempted"],
        "install_command": analysis_payload["meta"]["install_command"],
        "python_resolution": analysis_payload["meta"]["python_resolution"],
        "python_warning": analysis_payload["meta"]["python_warning"],
        "timestamp_utc": analysis_payload["meta"]["timestamp_utc"],
    },
    "failure_category": failure_category,
    "error_excerpt": error_excerpt,
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

pyright_stage_status="$("$PY_BOOTSTRAP" - <<PY
import json, pathlib
p = pathlib.Path(${RESULTS_JSON@Q})
try:
  d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("failure")
  raise SystemExit(0)
print(d.get("status","failure"))
PY
)"

if [[ "$pyright_stage_status" == "success" ]]; then
  exit 0
fi
exit 1
