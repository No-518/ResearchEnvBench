#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (rule: reportMissingImports).

Outputs (always, even on failure):
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
  --install-pyright              Install pyright into the selected environment if missing (default: on)
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes package)
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
install_pyright=1
pyright_extra_args=()

stage_status="failure"
stage_exit_code=1
skip_reason="not_applicable"
failure_category="unknown"
command_str=""
decision_reason=""
targets_str=""
install_attempted=0
install_cmd=""
pyright_exit_code=0
parse_error=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --install-pyright) install_pyright=1; shift ;;
    --no-install-pyright) install_pyright=0; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; pyright_extra_args=("$@"); break ;;
    *) parse_error="Unknown argument: $1"; break ;;
  esac
done

mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

touch "$log_file"

finalize() {
  [[ -s "$out_json" ]] || echo '{"generalDiagnostics":[],"summary":{"_note":"no_json_emitted"}}' >"$out_json"

  STATUS="$stage_status" SKIP_REASON="$skip_reason" EXIT_CODE="$stage_exit_code" \
    FAILURE_CATEGORY="$failure_category" OUT_DIR="$out_dir" MODE="$mode" REPO="$(pwd 2>/dev/null || echo "")" \
    PY_CMD="${py_cmd[*]:-}" COMMAND="$command_str" INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" \
    PYRIGHT_EXIT_CODE="$pyright_exit_code" TARGETS="$targets_str" DECISION_REASON="$decision_reason" \
    python3 - <<'PY' >/dev/null 2>&1 || true
import ast
import json
import os
import pathlib
import re
import subprocess
import time

out_dir = pathlib.Path(os.environ["OUT_DIR"])
log_file = out_dir / "log.txt"
out_json = out_dir / "pyright_output.json"
analysis_json = out_dir / "analysis.json"
results_json = out_dir / "results.json"

def tail(max_lines: int = 220) -> str:
    try:
        txt = log_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:]).strip()

pyright_data: dict = {}
parse_ok = True
try:
    pyright_data = json.loads(out_json.read_text(encoding="utf-8", errors="replace"))
except Exception:
    parse_ok = False
    pyright_data = {"_error": "invalid_json"}

diags = (pyright_data.get("generalDiagnostics", []) or []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diags if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pat = re.compile(r'Import\\s+\\"([^\\"]+)\\"')
missing_pkgs = set()
for d in missing_diags:
    msg = str(d.get("message", ""))
    m = pat.search(msg)
    if not m:
        continue
    pkg = m.group(1).split(".")[0]
    if pkg:
        missing_pkgs.add(pkg)

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

def iter_py_files(root: pathlib.Path):
    for p in root.rglob("*.py"):
        try:
            rel = p.relative_to(root)
        except Exception:
            continue
        if any(part in exclude_dirs for part in rel.parts):
            continue
        yield p

def imported_pkgs(py_file: pathlib.Path) -> set[str]:
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

repo_root = pathlib.Path(os.environ.get("REPO", "") or ".").resolve()
all_pkgs: set[str] = set()
files_scanned = 0
for f in iter_py_files(repo_root):
    files_scanned += 1
    all_pkgs |= imported_pkgs(f)

missing_list = sorted(missing_pkgs)
missing_count = len(missing_list)
total_imported = len(all_pkgs)
ratio = f"{missing_count}/{total_imported}"

def git_commit() -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""

status = os.environ.get("STATUS", "failure")
exit_code = int(os.environ.get("EXIT_CODE", "1"))
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown")

if not parse_ok and status != "failure":
    status = "failure"
    exit_code = 1
    failure_category = "invalid_json"

analysis = {
  "missing_packages": missing_list,
  "pyright": pyright_data,
  "meta": {
    "python": os.environ.get("PY_CMD",""),
    "python_cmd": os.environ.get("PY_CMD",""),
    "mode": os.environ.get("MODE",""),
    "repo": str(repo_root),
    "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE","0")),
    "pyright_install_attempted": os.environ.get("INSTALL_ATTEMPTED","0") == "1",
    "pyright_install_command": os.environ.get("INSTALL_CMD",""),
    "targets": os.environ.get("TARGETS",""),
    "decision_reason": os.environ.get("DECISION_REASON",""),
    "files_scanned": files_scanned,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "git_commit": git_commit(),
    "env_vars": {k: os.environ.get(k,"") for k in sorted(os.environ) if k.startswith("SCIMLOPSBENCH_")},
  },
  "metrics": {
    "missing_packages_count": missing_count,
    "total_imported_packages_count": total_imported,
    "missing_package_ratio": ratio,
  },
}

analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

payload = {
  "status": status,
  "skip_reason": os.environ.get("SKIP_REASON","not_applicable"),
  "exit_code": exit_code,
  "stage": "pyright",
  "task": "check",
  "command": os.environ.get("COMMAND",""),
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path":"", "source":"", "version":"", "sha256":""},
    "model": {"path":"", "source":"", "version":"", "sha256":""},
  },
  "missing_packages_count": missing_count,
  "total_imported_packages_count": total_imported,
  "missing_package_ratio": ratio,
  "meta": analysis["meta"],
  "failure_category": "not_applicable" if exit_code == 0 else failure_category,
  "error_excerpt": "" if exit_code == 0 else tail(),
}

results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

trap finalize EXIT

fail() {
  failure_category="$1"
  shift || true
  local msg="${1:-}"
  [[ -n "$msg" ]] && echo "[pyright] $msg" >>"$log_file"
  stage_status="failure"
  stage_exit_code=1
  exit 1
}

if [[ -n "$parse_error" ]]; then
  echo "[pyright] $parse_error" >>"$log_file"
  failure_category="args_unknown"
  stage_exit_code=1
  exit 1
fi

if [[ -z "$repo" ]]; then
  echo "--repo is required" >&2
  usage >&2
  failure_category="args_unknown"
  stage_exit_code=1
  exit 1
fi

cd "$repo"

{
  echo "[pyright] repo=$(pwd)"
  echo "[pyright] out_dir=$out_dir"
  echo "[pyright] mode=$mode"
  echo "[pyright] level=$pyright_level"
} >>"$log_file"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || fail "args_unknown" "--venv is required for --mode venv"
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || fail "args_unknown" "--conda-env is required for --mode conda"
      command -v conda >/dev/null 2>&1 || fail "deps" "conda not found in PATH"
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || fail "deps" "poetry not found in PATH"
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      fail "args_unknown" "Unknown --mode: $mode"
      ;;
  esac
fi

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_file" 2>&1; then
  fail "deps" "Failed to run python via: ${py_cmd[*]}"
fi

if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_file" 2>&1; then
  if [[ "$install_pyright" -eq 1 ]]; then
    install_attempted=1
    install_cmd="$(printf '%q ' "${py_cmd[@]}")-m pip install -q pyright"
    {
      echo "[pyright] installing pyright: $install_cmd"
    } >>"$log_file"
    if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_file" 2>&1; then
      # Best-effort categorization.
      if command -v rg >/dev/null 2>&1 && rg -n "(Temporary failure|Name or service not known|Connection|TLS|SSLError)" "$log_file" >/dev/null 2>&1; then
        fail "download_failed" "Failed to install pyright (likely offline/network blocked)"
      elif grep -E "(Temporary failure|Name or service not known|Connection|TLS|SSL)" "$log_file" >/dev/null 2>&1; then
        fail "download_failed" "Failed to install pyright (likely offline/network blocked)"
      fi
      fail "deps" "Failed to install pyright"
    fi
  else
    fail "deps" "pyright is not available (re-run with --install-pyright)"
  fi
fi

# Auto-detect project/targets (do not always run on ".")
targets=()
project_args=()

if [[ -f pyrightconfig.json ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="pyrightconfig.json"
elif [[ -f pyproject.toml ]] && grep -E "^[[:space:]]*\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  decision_reason="pyproject.toml:[tool.pyright]"
elif [[ -d src ]]; then
  targets=(src)
  [[ -d tests ]] && targets+=(tests)
  decision_reason="src_layout"
else
  mapfile -t targets < <(python3 - <<'PY'
import os
import pathlib

root = pathlib.Path(".").resolve()
exclude = {".git", "__pycache__", ".venv", "venv", "build", "dist", "node_modules", "build_output", "benchmark_assets"}

pkgs = set()
for p in root.rglob("__init__.py"):
    try:
        rel = p.relative_to(root)
    except Exception:
        continue
    if any(part in exclude for part in rel.parts):
        continue
    if len(rel.parts) >= 2:
        pkgs.add(rel.parts[0])

for name in sorted(pkgs):
    if (root / name).is_dir():
        print(name)
PY
  )
  if [[ ${#targets[@]} -gt 0 ]]; then
    decision_reason="package_dirs(__init__.py)"
  fi
fi

if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  fail "entrypoint_not_found" "Could not determine a Pyright target (no pyrightconfig/pyproject/src/package dirs found)"
fi

command_str="$(printf '%q ' "${py_cmd[@]}")-m pyright"
if [[ ${#project_args[@]} -gt 0 ]]; then
  command_str+=" $(printf '%q ' "${project_args[@]}")"
else
  command_str+=" $(printf '%q ' "${targets[@]}")"
fi
command_str+=" --level $(printf '%q' "$pyright_level") --outputjson"
if [[ ${#pyright_extra_args[@]} -gt 0 ]]; then
  command_str+=" $(printf '%q ' "${pyright_extra_args[@]}")"
fi

{
  echo "[pyright] command=$command_str"
  echo "[pyright] decision_reason=$decision_reason"
  echo "[pyright] targets=${targets[*]:-}"
} >>"$log_file"

pyright_exit_code=0
if [[ ${#project_args[@]} -gt 0 ]]; then
  "${py_cmd[@]}" -m pyright --level "$pyright_level" --outputjson "${project_args[@]}" "${pyright_extra_args[@]}" >"$out_json" 2>>"$log_file" || pyright_exit_code=$?
else
  "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json" 2>>"$log_file" || pyright_exit_code=$?
fi

if [[ ! -s "$out_json" ]]; then
  # Pyright might have crashed before emitting JSON.
  echo '{"generalDiagnostics":[],"summary":{"_note":"no_json_emitted"}}' >"$out_json"
fi

# Validate JSON output; if invalid, mark failure.
if ! python3 -c 'import json,sys; json.load(open(sys.argv[1], "r", encoding="utf-8"))' "$out_json" >/dev/null 2>&1; then
  stage_status="failure"
  stage_exit_code=1
  failure_category="invalid_json"
  exit 1
fi

stage_status="success"
stage_exit_code=0
failure_category="not_applicable"
targets_str="${targets[*]:-}"
exit 0
