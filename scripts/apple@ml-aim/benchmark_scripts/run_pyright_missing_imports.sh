#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

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
  --timeout-sec <int>            Default: 600
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonpath ...)

Examples:
  bash benchmark_scripts/run_pyright_missing_imports.sh --mode system --repo .
  bash benchmark_scripts/run_pyright_missing_imports.sh --python /abs/python --repo . -- --verifytypes aim
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
timeout_sec="600"
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
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
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

repo_abs="$(cd "$repo" && pwd)"
mkdir -p "$repo_abs/$out_dir"
log_txt="$repo_abs/$out_dir/log.txt"
out_json="$repo_abs/$out_dir/pyright_output.json"
analysis_json="$repo_abs/$out_dir/analysis.json"
results_json="$repo_abs/$out_dir/results.json"

{
  echo "[pyright] repo=$repo_abs"
  echo "[pyright] out_dir=$out_dir"
  echo "[pyright] mode=$mode"
  echo "[pyright] timeout_sec=$timeout_sec"
} >"$log_txt"

cd "$repo_abs"

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >>"$log_txt"
        echo '{}' >"$out_json"
        echo '{}' >"$analysis_json"
        echo "{\"status\":\"failure\",\"skip_reason\":\"unknown\",\"exit_code\":1,\"stage\":\"pyright\",\"task\":\"check\",\"command\":\"\",\"timeout_sec\":$timeout_sec,\"framework\":\"unknown\",\"assets\":{\"dataset\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"},\"model\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"}},\"meta\":{\"python\":\"\",\"git_commit\":\"\",\"env_vars\":{},\"decision_reason\":\"--venv missing\"},\"failure_category\":\"args_unknown\",\"error_excerpt\":\"--venv missing\"}" >"$results_json"
        exit 1
      fi
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >>"$log_txt"
        echo '{}' >"$out_json"
        echo '{}' >"$analysis_json"
        echo "{\"status\":\"failure\",\"skip_reason\":\"unknown\",\"exit_code\":1,\"stage\":\"pyright\",\"task\":\"check\",\"command\":\"\",\"timeout_sec\":$timeout_sec,\"framework\":\"unknown\",\"assets\":{\"dataset\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"},\"model\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"}},\"meta\":{\"python\":\"\",\"git_commit\":\"\",\"env_vars\":{},\"decision_reason\":\"--conda-env missing\"},\"failure_category\":\"args_unknown\",\"error_excerpt\":\"--conda-env missing\"}" >"$results_json"
        exit 1
      fi
      if ! command -v conda >/dev/null 2>&1; then
        echo "conda not found in PATH" >>"$log_txt"
        echo '{}' >"$out_json"
        echo '{}' >"$analysis_json"
        echo "{\"status\":\"failure\",\"skip_reason\":\"unknown\",\"exit_code\":1,\"stage\":\"pyright\",\"task\":\"check\",\"command\":\"\",\"timeout_sec\":$timeout_sec,\"framework\":\"unknown\",\"assets\":{\"dataset\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"},\"model\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"}},\"meta\":{\"python\":\"\",\"git_commit\":\"\",\"env_vars\":{},\"decision_reason\":\"conda not found\"},\"failure_category\":\"deps\",\"error_excerpt\":\"conda not found\"}" >"$results_json"
        exit 1
      fi
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      if ! command -v poetry >/dev/null 2>&1; then
        echo "poetry not found in PATH" >>"$log_txt"
        echo '{}' >"$out_json"
        echo '{}' >"$analysis_json"
        echo "{\"status\":\"failure\",\"skip_reason\":\"unknown\",\"exit_code\":1,\"stage\":\"pyright\",\"task\":\"check\",\"command\":\"\",\"timeout_sec\":$timeout_sec,\"framework\":\"unknown\",\"assets\":{\"dataset\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"},\"model\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"}},\"meta\":{\"python\":\"\",\"git_commit\":\"\",\"env_vars\":{},\"decision_reason\":\"poetry not found\"},\"failure_category\":\"deps\",\"error_excerpt\":\"poetry not found\"}" >"$results_json"
        exit 1
      fi
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >>"$log_txt"
      echo '{}' >"$out_json"
      echo '{}' >"$analysis_json"
      echo "{\"status\":\"failure\",\"skip_reason\":\"unknown\",\"exit_code\":1,\"stage\":\"pyright\",\"task\":\"check\",\"command\":\"\",\"timeout_sec\":$timeout_sec,\"framework\":\"unknown\",\"assets\":{\"dataset\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"},\"model\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"}},\"meta\":{\"python\":\"\",\"git_commit\":\"\",\"env_vars\":{},\"decision_reason\":\"unknown mode\"},\"failure_category\":\"args_unknown\",\"error_excerpt\":\"unknown mode\"}" >"$results_json"
      exit 1
      ;;
  esac
fi

{
  echo "[pyright] python_cmd=${py_cmd[*]}"
} >>"$log_txt"

python_ok=0
if "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >>"$log_txt" 2>&1; then
  python_ok=1
else
  python_ok=0
fi

if [[ "$python_ok" -ne 1 ]]; then
  echo "[pyright] failed to run python via: ${py_cmd[*]}" >>"$log_txt"
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  python3 - "$repo_abs" "$results_json" "$timeout_sec" "${py_cmd[*]}" "$mode" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
results_json = Path(sys.argv[2])
timeout_sec = int(sys.argv[3])
py_cmd = sys.argv[4]
mode = sys.argv[5]

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), text=True, timeout=5).strip()
    except Exception:
        return ""

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": py_cmd,
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": "",
        "git_commit": git_commit(),
        "env_vars": {},
        "decision_reason": f"Failed to execute python via mode={mode}",
        "pyright_install_attempted": False,
        "pyright_install_cmd": "",
    },
    "failure_category": "deps",
    "error_excerpt": "Failed to execute python command; see log.txt",
}
results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

install_attempted=0
install_cmd=""
if ! "${py_cmd[@]}" -c 'import pyright' >>"$log_txt" 2>&1; then
  install_attempted=1
  install_cmd="${py_cmd[*]} -m pip install -q pyright"
  echo "[pyright] pyright missing; attempting install: $install_cmd" >>"$log_txt"
  if ! "${py_cmd[@]}" -m pip install -q pyright >>"$log_txt" 2>&1; then
    echo "[pyright] pyright install failed" >>"$log_txt"
    echo '{}' >"$out_json"
    echo '{}' >"$analysis_json"
    python3 - "$repo_abs" "$results_json" "$timeout_sec" "${py_cmd[*]}" "$mode" "$install_attempted" "$install_cmd" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
results_json = Path(sys.argv[2])
timeout_sec = int(sys.argv[3])
py_cmd = sys.argv[4]
mode = sys.argv[5]
install_attempted = bool(int(sys.argv[6]))
install_cmd = sys.argv[7]

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), text=True, timeout=5).strip()
    except Exception:
        return ""

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "pyright",
    "task": "check",
    "command": install_cmd,
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": py_cmd,
        "git_commit": git_commit(),
        "env_vars": {},
        "decision_reason": f"Pyright missing; install failed in mode={mode}",
        "pyright_install_attempted": install_attempted,
        "pyright_install_cmd": install_cmd,
    },
    "failure_category": "download_failed",
    "error_excerpt": "Pyright installation failed; see log.txt",
}
results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
    exit 1
  fi
fi

target_args=()
targets=()

if [[ -f "pyrightconfig.json" ]]; then
  target_args=(--project "pyrightconfig.json")
elif [[ -f "pyproject.toml" ]] && rg -n --no-heading "^\\[tool\\.pyright\\]" "pyproject.toml" >/dev/null 2>&1; then
  target_args=(--project "pyproject.toml")
elif [[ -d "src" ]]; then
  targets=("src")
  [[ -d "tests" ]] && targets+=("tests")
else
  mapfile -t targets < <("${py_cmd[@]}" - <<'PY'
import os
from pathlib import Path

repo = Path(".").resolve()
exclude = {
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

init_files = []
for p in repo.rglob("__init__.py"):
    if any(part in exclude for part in p.parts):
        continue
    init_files.append(p)

pkg_roots = set()
for init in init_files:
    d = init.parent
    parent = d.parent
    if (parent / "__init__.py").exists():
        continue
    # Prefer top-level packages under subprojects (e.g., aim-v1/aim, aim-v2/aim)
    pkg_roots.add(str(d.relative_to(repo)))

for d in sorted(pkg_roots):
    print(d)
PY
  )
fi

if [[ "${#target_args[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  echo "[pyright] unable to detect targets (no pyrightconfig/pyproject/src/package dirs)" >>"$log_txt"
  echo '{}' >"$out_json"
  echo '{}' >"$analysis_json"
  python3 - "$repo_abs" "$results_json" "$timeout_sec" "${py_cmd[*]}" "$mode" "$install_attempted" "$install_cmd" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
results_json = Path(sys.argv[2])
timeout_sec = int(sys.argv[3])
py_cmd = sys.argv[4]
mode = sys.argv[5]
install_attempted = bool(int(sys.argv[6]))
install_cmd = sys.argv[7]

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), text=True, timeout=5).strip()
    except Exception:
        return ""

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
    "meta": {
        "python": py_cmd,
        "git_commit": git_commit(),
        "env_vars": {},
        "decision_reason": "Unable to detect python targets for pyright",
        "pyright_install_attempted": install_attempted,
        "pyright_install_cmd": install_cmd,
    },
    "failure_category": "entrypoint_not_found",
    "error_excerpt": "No pyright targets could be detected",
}
results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

pyright_cmd=("${py_cmd[@]}" -m pyright)
if [[ "${#target_args[@]}" -gt 0 ]]; then
  pyright_cmd+=("${target_args[@]}")
else
  pyright_cmd+=("${targets[@]}")
fi
pyright_cmd+=(--level "$pyright_level" --outputjson)
if [[ "${#pyright_extra_args[@]}" -gt 0 ]]; then
  pyright_cmd+=("${pyright_extra_args[@]}")
fi

{
  echo "[pyright] running: ${pyright_cmd[*]}"
} >>"$log_txt"

pyright_exit_code=0
if ! "${pyright_cmd[@]}" >"$out_json" 2>>"$log_txt"; then
  pyright_exit_code=$?
fi
echo "[pyright] pyright_exit_code=$pyright_exit_code (non-zero does NOT fail the stage)" >>"$log_txt"

if [[ ! -s "$out_json" ]]; then
  echo '{}' >"$out_json"
fi

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" REPO_ROOT="$repo_abs" \
MODE="$mode" PY_CMD="${py_cmd[*]}" PYRIGHT_CMD="${pyright_cmd[*]}" PYRIGHT_EXIT_CODE="$pyright_exit_code" \
INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" TIMEOUT_SEC="$timeout_sec" \
TARGETS="${targets[*]}" PROJECT_ARGS="${target_args[*]}" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable

repo_root = Path(os.environ["REPO_ROOT"]).resolve()
out_json = Path(os.environ["OUT_JSON"])
analysis_json = Path(os.environ["ANALYSIS_JSON"])
results_json = Path(os.environ["RESULTS_JSON"])
timeout_sec = int(os.environ.get("TIMEOUT_SEC", "600"))

def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=5
        ).strip()
    except Exception:
        return ""

def tail_log(log_path: Path, max_lines: int = 200) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

data = {}
try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^"]+)"')
missing_packages = sorted(
    {
        (m.group(1).split(".")[0] if m else "")
        for d in missing_diags
        if (m := pattern.search(d.get("message", "")))
    }
    - {""}
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

targets_str = os.environ.get("TARGETS", "").strip()
project_args = os.environ.get("PROJECT_ARGS", "").strip()
targets: list[Path] = []
if targets_str:
    for part in targets_str.split():
        p = (repo_root / part).resolve()
        if p.exists():
            targets.append(p)
else:
    # If using --project, scan repo root but still respect excludes.
    targets = [repo_root]

def iter_py_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: Path) -> set[str]:
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
for t in targets:
    for py_file in iter_py_files(t):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis_payload = {
    "missing_packages": missing_packages,
    "missing_import_diagnostics": missing_diags,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "pyright_cmd": os.environ.get("PYRIGHT_CMD", ""),
        "pyright_exit_code": int(os.environ.get("PYRIGHT_EXIT_CODE", "0") or 0),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0") or 0)),
        "install_cmd": os.environ.get("INSTALL_CMD", ""),
        "targets": [str(p) for p in targets],
        "project_args": project_args,
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}
analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

results_payload = {
    "status": "success",
    "skip_reason": "unknown",
    "exit_code": 0,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "missing_packages_count": missing_packages_count,
    "total_imported_packages_count": total_imported_packages_count,
    "missing_package_ratio": missing_package_ratio,
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD", ""),
        "git_commit": git_commit(),
        "env_vars": {},
        "decision_reason": "Static missing-import detection with Pyright",
        "pyright_install_attempted": analysis_payload["meta"]["install_attempted"],
        "pyright_install_cmd": analysis_payload["meta"]["install_cmd"],
        "pyright_exit_code": analysis_payload["meta"]["pyright_exit_code"],
        "targets": analysis_payload["meta"]["targets"],
        "project_args": analysis_payload["meta"]["project_args"],
    },
    "failure_category": "unknown",
    "error_excerpt": "",
}
results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

exit 0

