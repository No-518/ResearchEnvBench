#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics (reportMissingImports).

Default interpreter resolution (if neither --python nor --mode is provided):
  1) $SCIMLOPSBENCH_PYTHON (if set)
  2) "python_path" from /opt/scimlopsbench/report.json (or $SCIMLOPSBENCH_REPORT)
  3) python3/python from PATH (last resort; recorded as warning)

Environment selection (optional):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --repo <path>                  Repository root (default: git root or script parent)
  --out-dir <path>               Output directory (default: <repo>/build_output/pyright)
  --level <error|warning|...>    Default: error
  -- <pyright args...>           Extra args passed to Pyright

Outputs (always written, even on failure):
  <out-dir>/log.txt
  <out-dir>/pyright_output.json
  <out-dir>/analysis.json
  <out-dir>/results.json
EOF
}

mode=""
repo=""
out_dir=""
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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$repo" ]]; then
  if command -v git >/dev/null 2>&1; then
    repo="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null || true)"
  fi
  if [[ -z "$repo" ]]; then
    repo="$(cd "$SCRIPT_DIR/.." && pwd)"
  fi
fi

if [[ -z "$out_dir" ]]; then
  out_dir="$repo/build_output/pyright"
fi

mkdir -p "$out_dir"

export REPO="$repo"
export OUT="$out_dir"

log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

touch "$log_file"
touch "$out_json"
touch "$analysis_json"

{
  echo "[pyright] repo=$repo"
  echo "[pyright] out_dir=$out_dir"
  echo "[pyright] started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >>"$log_file"

export PYTHONDONTWRITEBYTECODE=1
export XDG_CACHE_HOME="$repo/benchmark_assets/cache/xdg"
export PIP_CACHE_DIR="$repo/benchmark_assets/cache/pip"
export HOME="$repo/benchmark_assets/cache/home"
export TMPDIR="$repo/benchmark_assets/cache/tmp"
mkdir -p "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" "$HOME" "$TMPDIR"

HAS_RG=0
if command -v rg >/dev/null 2>&1; then
  HAS_RG=1
fi

resolve_python() {
  local report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

  if [[ -n "$python_bin" ]]; then
    echo "$python_bin"
    return 0
  fi

  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "${SCIMLOPSBENCH_PYTHON}"
    return 0
  fi

  if [[ -n "$mode" ]]; then
    case "$mode" in
      venv)
        if [[ -z "$venv_dir" ]]; then
          echo "ERROR: --venv is required for --mode venv" >&2
          return 2
        fi
        echo "$venv_dir/bin/python"
        return 0
        ;;
      uv)
        venv_dir="${venv_dir:-$repo/.venv}"
        echo "$venv_dir/bin/python"
        return 0
        ;;
      conda)
        if [[ -z "$conda_env" ]]; then
          echo "ERROR: --conda-env is required for --mode conda" >&2
          return 2
        fi
        if ! command -v conda >/dev/null 2>&1; then
          echo "ERROR: conda not found in PATH" >&2
          return 2
        fi
        echo "conda run -n $conda_env python"
        return 0
        ;;
      poetry)
        if ! command -v poetry >/dev/null 2>&1; then
          echo "ERROR: poetry not found in PATH" >&2
          return 2
        fi
        echo "poetry run python"
        return 0
        ;;
      system)
        echo "python"
        return 0
        ;;
      *)
        echo "ERROR: Unknown --mode: $mode" >&2
        return 2
        ;;
    esac
  fi

  # Default: use report.json python_path
  if [[ ! -f "$report_path" ]]; then
    echo "ERROR: missing report.json at $report_path (provide --python or --mode ...)" >&2
    return 1
  fi

  local py_json
  if command -v python3 >/dev/null 2>&1; then
    py_json="python3"
  elif command -v python >/dev/null 2>&1; then
    py_json="python"
  else
    echo "ERROR: python3/python not found to parse report.json (provide --python or --mode ...)" >&2
    return 1
  fi

  local rp
  rp="$("$py_json" - <<PY 2>>"$log_file" || true
import json, pathlib
p = pathlib.Path(${report_path@Q})
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  data = {}
print(data.get("python_path","") or "")
PY
)"
  if [[ -z "$rp" ]]; then
    echo "ERROR: report.json missing/invalid python_path at $report_path (provide --python or --mode ...)" >&2
    return 1
  fi
  echo "$rp"
  return 0
}

PY_CMD_STR="$(resolve_python 2>>"$log_file" || true)"
if [[ -z "$PY_CMD_STR" ]]; then
  {
    echo "[pyright] failed to resolve python"
    echo "[pyright] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >>"$log_file"
  echo "{}" >"$out_json"
  python3 - <<'PY' >"$analysis_json"
import json, os, time
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO","")
data={
  "missing_packages":[],
  "pyright":{},
  "meta":{"timestamp_utc":utc(),"repo":repo,"error":"Failed to resolve python interpreter"},
  "metrics":{"missing_packages_count":0,"total_imported_packages_count":0,"missing_package_ratio":"0/0"},
}
print(json.dumps(data, indent=2))
PY
  python3 - <<'PY' >"$results_json"
import json, os, time
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO","")
out=os.environ.get("OUT","")
data={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{
    "dataset":{"path":os.path.join(repo,"benchmark_assets","dataset"),"source":"not_applicable","version":"unknown","sha256":""},
    "model":{"path":os.path.join(repo,"benchmark_assets","model"),"source":"not_applicable","version":"unknown","sha256":""},
  },
  "meta":{
    "python":"",
    "git_commit":"",
    "env_vars":{},
    "decision_reason":"Failed to resolve python interpreter for pyright stage.",
    "timestamp_utc":utc(),
  },
  "failure_category":"missing_report",
  "error_excerpt":"Failed to resolve python interpreter"
}
print(json.dumps(data, indent=2))
PY
  exit 1
fi

export PY_CMD="$PY_CMD_STR"

cd "$repo" || exit 1

IFS=' ' read -r -a PY_CMD_ARR <<<"$PY_CMD_STR"

{
  echo "[pyright] mode=${mode:-<default-report>}"
  echo "[pyright] python_cmd=${PY_CMD_STR}"
  echo "[pyright] level=$pyright_level"
} >>"$log_file"

python_ok=0
if "${PY_CMD_ARR[@]}" -c 'import sys; print(sys.executable)' >>"$log_file" 2>&1; then
  python_ok=1
fi
if [[ "$python_ok" -ne 1 ]]; then
  {
    echo "[pyright] Failed to run python via: ${PY_CMD_STR}"
    echo "[pyright] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >>"$log_file"
  echo "{}" >"$out_json"
  python3 - <<'PY' >"$analysis_json"
import json, os, time
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO","")
data={
  "missing_packages":[],
  "pyright":{},
  "meta":{"timestamp_utc":utc(),"repo":repo,"error":"Resolved python could not be executed"},
  "metrics":{"missing_packages_count":0,"total_imported_packages_count":0,"missing_package_ratio":"0/0"},
}
print(json.dumps(data, indent=2))
PY
  python3 - <<'PY' >"$results_json"
import json, os, time
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO","")
cmd=os.environ.get("PY_CMD","")
data={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":cmd,
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{
    "dataset":{"path":os.path.join(repo,"benchmark_assets","dataset"),"source":"not_applicable","version":"unknown","sha256":""},
    "model":{"path":os.path.join(repo,"benchmark_assets","model"),"source":"not_applicable","version":"unknown","sha256":""},
  },
  "meta":{
    "python":cmd,
    "git_commit":"",
    "env_vars":{},
    "decision_reason":"Resolved python could not be executed.",
    "timestamp_utc":utc(),
  },
  "failure_category":"path_hallucination",
  "error_excerpt":"Resolved python could not be executed."
}
print(json.dumps(data, indent=2))
PY
  exit 1
fi

install_attempted=0
install_cmd=""
if ! "${PY_CMD_ARR[@]}" -c 'import pyright' >>"$log_file" 2>&1; then
  install_attempted=1
  install_cmd="${PY_CMD_STR} -m pip install -q pyright"
  echo "[pyright] pyright missing; attempting install: $install_cmd" >>"$log_file"
  if ! "${PY_CMD_ARR[@]}" -m pip install -q pyright >>"$log_file" 2>&1; then
    echo "[pyright] pyright installation failed" >>"$log_file"
    # still attempt to run pyright if module exists via entrypoint (best effort)
  fi
fi

project_args=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
elif [[ -f "pyproject.toml" ]] && { [[ "$HAS_RG" -eq 1 ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1 || [[ "$HAS_RG" -eq 0 ]] && grep -q "^\\[tool\\.pyright\\]" pyproject.toml; }; then
  project_args=(--project pyproject.toml)
elif [[ -d "src" ]]; then
  targets+=(src)
  [[ -d "tests" ]] && targets+=(tests)
  [[ -d "test" ]] && targets+=(test)
else
  shopt -s nullglob
  for init_file in ./*/__init__.py; do
    pkg_dir="$(dirname "$init_file")"
    base="$(basename "$pkg_dir")"
    case "$base" in
      .git|build_output|benchmark_assets|media|docs|patches|benchmark|scripts|dist|build|__pycache__)
        continue ;;
    esac
    targets+=("$pkg_dir")
  done
  shopt -u nullglob
fi

if [[ ${#project_args[@]} -eq 0 && ${#targets[@]} -eq 0 ]]; then
  echo "[pyright] ERROR: no pyright project/targets detected" >>"$log_file"
  echo "{}" >"$out_json"
  python3 - <<'PY' >"$analysis_json"
import json, os, time
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO","")
data={
  "missing_packages":[],
  "pyright":{},
  "meta":{
    "timestamp_utc":utc(),
    "repo":repo,
    "error":"No pyright project/targets detected",
  },
  "metrics":{
    "missing_packages_count":0,
    "total_imported_packages_count":0,
    "missing_package_ratio":"0/0",
  }
}
print(json.dumps(data, indent=2))
PY
  python3 - <<'PY' >"$results_json"
import json, os, time
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO","")
data={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"pyright",
  "task":"check",
  "command":"pyright (auto-detect targets)",
  "timeout_sec":600,
  "framework":"unknown",
  "assets":{
    "dataset":{"path":os.path.join(repo,"benchmark_assets","dataset"),"source":"not_applicable","version":"unknown","sha256":""},
    "model":{"path":os.path.join(repo,"benchmark_assets","model"),"source":"not_applicable","version":"unknown","sha256":""},
  },
  "meta":{
    "python":os.environ.get("PY_CMD",""),
    "git_commit":"",
    "env_vars":{},
    "decision_reason":"No pyright project/targets detected via pyrightconfig/pyproject/src/__init__.py scan.",
    "timestamp_utc":utc(),
    "pyright_install_attempted":bool(int(os.environ.get("INSTALL_ATTEMPTED","0"))),
    "pyright_install_command":os.environ.get("INSTALL_CMD",""),
  },
  "failure_category":"entrypoint_not_found",
  "error_excerpt":"No pyright project/targets detected"
}
print(json.dumps(data, indent=2))
PY
  exit 1
fi

echo "[pyright] project_args=${project_args[*]:-<none>}" >>"$log_file"
echo "[pyright] targets=${targets[*]:-<none>}" >>"$log_file"
echo "[pyright] extra_args=${pyright_extra_args[*]:-<none>}" >>"$log_file"

# Always produce JSON output even if Pyright exits non-zero.
pyright_exit_code=0
if [[ ${#project_args[@]} -gt 0 ]]; then
  "${PY_CMD_ARR[@]}" -m pyright "${project_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json" 2>>"$log_file" || pyright_exit_code=$?
else
  "${PY_CMD_ARR[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json" 2>>"$log_file" || pyright_exit_code=$?
fi

echo "[pyright] pyright_exit_code=$pyright_exit_code" >>"$log_file"

export OUT_JSON="$out_json"
export ANALYSIS_JSON="$analysis_json"
export RESULTS_JSON="$results_json"
export MODE="${mode:-default-report}"
export PY_CMD="${PY_CMD_STR}"
export INSTALL_ATTEMPTED="$install_attempted"
export INSTALL_CMD="$install_cmd"
export PYRIGHT_EXIT_CODE="$pyright_exit_code"
export TARGETS="${targets[*]:-}"
export PROJECT_ARGS="${project_args[*]:-}"
export REPO_ROOT="$repo"
export OUT_DIR="$out_dir"

python3 - <<'PY' >>"$log_file" 2>&1
import ast
import json
import os
import pathlib
import re
import time
from typing import Iterable, List, Set

def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

repo_root = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
out_json = pathlib.Path(os.environ["OUT_JSON"]).resolve()
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"]).resolve()
results_json = pathlib.Path(os.environ["RESULTS_JSON"]).resolve()

mode = os.environ.get("MODE", "")
py_cmd = os.environ.get("PY_CMD", "")
install_attempted = os.environ.get("INSTALL_ATTEMPTED", "0") == "1"
install_cmd = os.environ.get("INSTALL_CMD", "")
pyright_exit_code = int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or "0")
targets_str = os.environ.get("TARGETS", "").strip()
project_args = os.environ.get("PROJECT_ARGS", "").strip()

def read_pyright_json() -> dict:
    try:
        raw = out_json.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return {}
        return json.loads(raw)
    except Exception as e:
        return {"__parse_error__": str(e)}

data = read_pyright_json()
diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]
pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
)

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
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if root.is_dir():
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

roots: List[pathlib.Path] = []
if targets_str:
    for t in targets_str.split():
        roots.append((repo_root / t).resolve())
else:
    roots.append(repo_root)

all_imported_packages: Set[str] = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": mode,
        "python_cmd": py_cmd,
        "pyright_exit_code": pyright_exit_code,
        "pyright_project_args": project_args,
        "targets": targets_str,
        "files_scanned": files_scanned,
        "pyright_install_attempted": install_attempted,
        "pyright_install_command": install_cmd,
        "timestamp_utc": utc(),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(
    json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(f"[pyright] metrics missing_package_ratio={missing_package_ratio}")

PY

py_parse_ok=1
if ! python3 -c "import json; json.load(open('${out_json}', 'r', encoding='utf-8'))" >/dev/null 2>&1; then
  py_parse_ok=0
fi

missing_report=0
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
if [[ ! -f "$report_path" ]] && [[ -z "$python_bin" ]] && [[ -z "$mode" ]] && [[ -z "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  missing_report=1
fi

git_commit=""
if command -v git >/dev/null 2>&1; then
  git_commit="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
fi

status="success"
failure_category="unknown"
stage_exit=0

if [[ "$missing_report" -eq 1 ]]; then
  status="failure"
  failure_category="missing_report"
  stage_exit=1
elif [[ "$install_attempted" -eq 1 ]] && ! "${PY_CMD_ARR[@]}" -c 'import pyright' >>"$log_file" 2>&1; then
  status="failure"
  failure_category="deps"
  stage_exit=1
elif [[ "$py_parse_ok" -eq 0 ]]; then
  status="failure"
  failure_category="invalid_json"
  stage_exit=1
else
  # pyright_exit_code != 0 does not necessarily indicate missing imports; keep success.
  status="success"
  failure_category="unknown"
  stage_exit=0
fi

error_excerpt="$(tail -n 220 "$log_file" 2>/dev/null | sed -n '1,220p')"

{
  echo "[pyright] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >>"$log_file"

export STAGE_STATUS="$status"
export FAILURE_CATEGORY="$failure_category"
export STAGE_EXIT="$stage_exit"
export ERROR_EXCERPT="$error_excerpt"
export GIT_COMMIT="$git_commit"
if [[ ${#project_args[@]} -gt 0 ]]; then
  export PYRIGHT_COMMAND="${PY_CMD_STR} -m pyright ${project_args[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
else
  export PYRIGHT_COMMAND="${PY_CMD_STR} -m pyright ${targets[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
fi

py_render="python3"
if ! command -v python3 >/dev/null 2>&1; then
  py_render="python"
fi

"$py_render" - <<'PY' >"$results_json"
import json, os, time, pathlib
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo=os.environ.get("REPO_ROOT","")
analysis_path=pathlib.Path(os.environ.get("ANALYSIS_JSON",""))
metrics={}
try:
  data=json.loads(analysis_path.read_text(encoding="utf-8"))
  metrics=data.get("metrics",{}) if isinstance(data,dict) else {}
except Exception:
  metrics={}
payload={
  "status": os.environ.get("STAGE_STATUS","failure"),
  "skip_reason":"unknown",
  "exit_code": int(os.environ.get("STAGE_EXIT","1")),
  "stage":"pyright",
  "task":"check",
  "command": os.environ.get("PYRIGHT_COMMAND",""),
  "timeout_sec": 600,
  "framework":"unknown",
  "assets":{
    "dataset":{"path":str(pathlib.Path(repo,"benchmark_assets","dataset").resolve()),"source":"not_applicable","version":"unknown","sha256":""},
    "model":{"path":str(pathlib.Path(repo,"benchmark_assets","model").resolve()),"source":"not_applicable","version":"unknown","sha256":""},
  },
  "meta":{
    "python": os.environ.get("PY_CMD",""),
    "git_commit": os.environ.get("GIT_COMMIT",""),
    "env_vars": {k:os.environ.get(k,"") for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","PYTHONDONTWRITEBYTECODE","PIP_CACHE_DIR","XDG_CACHE_HOME","HOME","TMPDIR"]},
    "decision_reason":"Auto-detected Pyright targets: pyrightconfig.json > pyproject [tool.pyright] > src/ > package dirs with __init__.py.",
    "timestamp_utc": utc(),
    "pyright_install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED","0"))),
    "pyright_install_command": os.environ.get("INSTALL_CMD",""),
    "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE","0") or "0"),
    "pyright_project_args": os.environ.get("PROJECT_ARGS",""),
    "targets": os.environ.get("TARGETS",""),
  },
  "metrics": {
    "missing_packages_count": int(metrics.get("missing_packages_count",0) or 0),
    "total_imported_packages_count": int(metrics.get("total_imported_packages_count",0) or 0),
    "missing_package_ratio": metrics.get("missing_package_ratio","0/0"),
  },
  "failure_category": os.environ.get("FAILURE_CATEGORY","unknown"),
  "error_excerpt": os.environ.get("ERROR_EXCERPT",""),
}
print(json.dumps(payload, indent=2))
PY

exit "$stage_exit"
