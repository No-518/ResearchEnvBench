#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Python selection (highest priority first):
  --python <path>                Explicit python executable to use
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

If none of the above is provided, the script will try:
  - Env var SCIMLOPSBENCH_PYTHON
  - python_path from /opt/scimlopsbench/report.json (or SCIMLOPSBENCH_REPORT)
  - python from PATH (last resort)

Required:
  --repo <path>                  Path to repository root

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright

Examples:
  ./benchmark_scripts/run_pyright_missing_imports.sh --repo ./
  ./benchmark_scripts/run_pyright_missing_imports.sh --mode venv --venv .venv --repo ./
  ./benchmark_scripts/run_pyright_missing_imports.sh --python /abs/path/to/python --repo ./ -- --verifytypes depth_anything_v2
EOF
}

stage="pyright"
task="check"
framework="unknown"
timeout_sec=600

repo=""
out_dir="build_output/pyright"
pyright_level="error"

mode=""
python_bin=""
venv_dir=""
conda_env=""

pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
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

if [[ -z "$repo" ]]; then
  echo "--repo is required" >&2
  usage
  exit 2
fi

repo="$(cd "$repo" && pwd)"
out_dir="$repo/$out_dir"
mkdir -p "$out_dir"

log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

status="failure"
skip_reason="unknown"
exit_code=1
failure_category="unknown"
decision_reason=""
command_str=""
install_attempted=0
install_command=""
install_returncode=""
pyright_returncode=""

git_commit=""
if command -v git >/dev/null 2>&1; then
  git_commit="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
fi

py_cmd=()

resolve_python_from_report() {
  local report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  python - <<PY 2>/dev/null || true
import json, os, pathlib, sys
p = pathlib.Path(${report_path@Q})
if not p.exists():
    sys.exit(0)
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
v = data.get("python_path")
if isinstance(v, str) and v.strip():
    print(v.strip())
PY
}

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  decision_reason="python selected via --python"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("${SCIMLOPSBENCH_PYTHON}")
  decision_reason="python selected via SCIMLOPSBENCH_PYTHON"
elif [[ -n "$mode" ]]; then
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      decision_reason="python selected via --mode venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      decision_reason="python selected via --mode uv"
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      decision_reason="python selected via --mode conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      decision_reason="python selected via --mode poetry"
      ;;
    system)
      py_cmd=(python)
      decision_reason="python selected via --mode system"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      exit 2
      ;;
  esac
else
  report_python="$(resolve_python_from_report)"
  if [[ -n "$report_python" ]]; then
    py_cmd=("$report_python")
    decision_reason="python selected via report.json python_path"
  else
    py_cmd=(python)
    decision_reason="python selected via PATH fallback (no mode/override/report)"
  fi
fi

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"
echo "[pyright] python_cmd=${py_cmd[*]}"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  failure_category="missing_report"
  status="failure"
  exit_code=1
  printf '{}' >"$out_json"
  printf '{}' >"$analysis_json"
  printf '%s\n' "$(python - <<PY
import json
print(json.dumps({
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "git_commit": ${git_commit@Q}, "env_vars": {"SCIMLOPSBENCH_REPORT": "${SCIMLOPSBENCH_REPORT:-}", "SCIMLOPSBENCH_PYTHON": "${SCIMLOPSBENCH_PYTHON:-}", "CUDA_VISIBLE_DEVICES": "${CUDA_VISIBLE_DEVICES:-}"}, "decision_reason": ${decision_reason@Q}},
  "failure_category": "missing_report",
  "error_excerpt": ""
}, indent=2))
PY)" >"$results_json"
  exit 1
fi

# Detect Pyright target/project automatically (do not always run on ".").
project_args=()
targets=()
if [[ -f "$repo/pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="$decision_reason; using pyrightconfig.json"
elif [[ -f "$repo/pyproject.toml" ]] && grep -qE '^\[tool\.pyright\]' "$repo/pyproject.toml" 2>/dev/null; then
  project_args=(--project pyproject.toml)
  decision_reason="$decision_reason; using pyproject.toml [tool.pyright]"
elif [[ -d "$repo/src" ]]; then
  targets=("src")
  [[ -d "$repo/tests" ]] && targets+=("tests")
  decision_reason="$decision_reason; using src/ layout targets"
else
  mapfile -t init_files < <(find "$repo" -type f -name '__init__.py' \
    -not -path '*/.git/*' \
    -not -path '*/.venv/*' \
    -not -path '*/venv/*' \
    -not -path '*/node_modules/*' \
    -not -path '*/build_output/*' \
    -not -path '*/benchmark_assets/*' \
    -not -path '*/benchmark_scripts/*' \
    2>/dev/null | sed "s|^$repo/||" | sort -u)
  for f in "${init_files[@]}"; do
    d="$(dirname "$f")"
    targets+=("$d")
  done
  if [[ "${#targets[@]}" -gt 0 ]]; then
    decision_reason="$decision_reason; using detected __init__.py package dirs"
  fi
fi

if [[ "${#project_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  failure_category="entrypoint_not_found"
  status="failure"
  exit_code=1
  printf '{}' >"$out_json"
  printf '%s\n' '{"missing_packages":[],"pyright":{},"meta":{},"metrics":{"missing_packages_count":0,"total_imported_packages_count":0,"missing_package_ratio":"0/0"}}' >"$analysis_json"
  printf '%s\n' "$(python - <<PY
import json
print(json.dumps({
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "${py_cmd[*]}", "git_commit": ${git_commit@Q}, "env_vars": {"SCIMLOPSBENCH_REPORT": "${SCIMLOPSBENCH_REPORT:-}", "SCIMLOPSBENCH_PYTHON": "${SCIMLOPSBENCH_PYTHON:-}", "CUDA_VISIBLE_DEVICES": "${CUDA_VISIBLE_DEVICES:-}"}, "decision_reason": ${decision_reason@Q}},
  "failure_category": "entrypoint_not_found",
  "error_excerpt": ""
}, indent=2))
PY)" >"$results_json"
  exit 1
fi

# Ensure pyright is available in the selected interpreter. Install if missing (mandatory).
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_command="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] Installing pyright: $install_command"
  "${py_cmd[@]}" -m pip install -q pyright
  install_returncode="$?"
  if [[ "$install_returncode" != "0" ]]; then
    # Categorize roughly: download issues vs general deps.
    if grep -qE "Temporary failure|Name or service not known|ConnectionError|Read timed out|SSLError|CERTIFICATE_VERIFY_FAILED" "$log_path" 2>/dev/null; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    status="failure"
    exit_code=1
    printf '{}' >"$out_json"
    printf '{}' >"$analysis_json"
    printf '%s\n' "$(python - <<PY
import json
print(json.dumps({
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "pyright",
  "task": "check",
  "command": ${install_command@Q},
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "${py_cmd[*]}", "git_commit": ${git_commit@Q}, "env_vars": {"SCIMLOPSBENCH_REPORT": "${SCIMLOPSBENCH_REPORT:-}", "SCIMLOPSBENCH_PYTHON": "${SCIMLOPSBENCH_PYTHON:-}", "CUDA_VISIBLE_DEVICES": "${CUDA_VISIBLE_DEVICES:-}"}, "decision_reason": ${decision_reason@Q}, "pyright_install_attempted": True, "pyright_install_command": ${install_command@Q}, "pyright_install_returncode": ${install_returncode@Q}},
  "failure_category": ${failure_category@Q},
  "error_excerpt": ""
}, indent=2))
PY)" >"$results_json"
    exit 1
  fi
fi

mkdir -p "$out_dir"

command=("${py_cmd[@]}" -m pyright)
if [[ "${#project_args[@]}" -gt 0 ]]; then
  command+=("${project_args[@]}")
else
  command+=("${targets[@]}")
fi
command+=(--level "$pyright_level" --outputjson)
if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
  command+=("${pyright_extra_args[@]}")
fi
command_str="$(printf '%q ' "${command[@]}")"
echo "[pyright] Running: $command_str"

"${command[@]}" >"$out_json"
pyright_returncode="$?"
echo "[pyright] pyright_returncode=$pyright_returncode"

# Analyze only reportMissingImports diagnostics and estimate imported package set via AST scan.
OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" \
REPO_ROOT="$repo" PY_CMD="${py_cmd[*]}" MODE="${mode:-auto}" \
DECISION_REASON="$decision_reason" INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" \
INSTALL_RET="$install_returncode" PYRIGHT_RET="$pyright_returncode" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

def safe_read_json(p: pathlib.Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

pyright_data = safe_read_json(out_json)
diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []

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
    "pyright": pyright_data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright_install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "pyright_install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_install_returncode": os.environ.get("INSTALL_RET", ""),
        "pyright_returncode": os.environ.get("PYRIGHT_RET", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

python - <<PY
import json
import os
import pathlib
import subprocess

repo = pathlib.Path(${repo@Q})
out_json = pathlib.Path(${out_json@Q})
analysis_json = pathlib.Path(${analysis_json@Q})
results_json = pathlib.Path(${results_json@Q})
log_path = pathlib.Path(${log_path@Q})

def tail(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])

def safe_read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

analysis = safe_read_json(analysis_json) or {}
metrics = analysis.get("metrics") if isinstance(analysis, dict) else None
if not isinstance(metrics, dict):
    metrics = {"missing_packages_count": 0, "total_imported_packages_count": 0, "missing_package_ratio": "0/0"}

py_ver = ""
try:
    cp = subprocess.run(
        ${py_cmd[*]@Q}.split() + ["-c", "import platform; print(platform.python_version())"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if cp.returncode == 0:
        py_ver = cp.stdout.strip()
except Exception:
    pass

pyright_data = safe_read_json(out_json)
pyright_parse_ok = isinstance(pyright_data, dict)

status = "success" if pyright_parse_ok else "failure"
exit_code = 0 if pyright_parse_ok else 1
failure_category = "" if pyright_parse_ok else "runtime"

payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": ${stage@Q},
    "task": ${task@Q},
    "command": ${command_str@Q},
    "timeout_sec": int(${timeout_sec}),
    "framework": ${framework@Q},
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": f"{${py_cmd[*]@Q}} ({py_ver})",
        "git_commit": ${git_commit@Q},
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        },
        "decision_reason": ${decision_reason@Q},
        "pyright_install_attempted": bool(int(${install_attempted})),
        "pyright_install_command": ${install_command@Q},
        "pyright_install_returncode": ${install_returncode@Q},
        "pyright_returncode": ${pyright_returncode@Q},
        "pyright_output_parse_ok": pyright_parse_ok,
    },
    "metrics": metrics,
    "failure_category": failure_category,
    "error_excerpt": tail(log_path),
}

results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
raise SystemExit(exit_code)
PY
