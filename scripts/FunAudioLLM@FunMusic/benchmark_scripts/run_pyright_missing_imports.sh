#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

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
  --mode system                  Use: python from PATH

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <n>              Default: 600 (best-effort; does not hard-kill child processes)
  -- <pyright args...>           Extra args passed to Pyright (e.g. --verifytypes pkg)

Examples:
  ./benchmark_scripts/run_pyright_missing_imports.sh --repo . --mode system
  ./benchmark_scripts/run_pyright_missing_imports.sh --repo . --python /abs/path/to/python
EOF
}

mode="system"
repo=""
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
timeout_sec=600
pyright_extra_args=()
util_python=""

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
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
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

if command -v python >/dev/null 2>&1; then
  util_python="python"
elif command -v python3 >/dev/null 2>&1; then
  util_python="python3"
else
  echo "ERROR: python (or python3) not found in PATH" >&2
  exit 1
fi

repo="$("$util_python" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$repo")"

mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

exec > >(tee "$log_path") 2>&1

cd "$repo"

py_cmd=()
install_attempted=0
install_cmd=""
python_resolution="cli/mode"

if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      if [[ -z "$venv_dir" ]]; then
        echo "--venv is required for --mode venv" >&2
        exit 2
      fi
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      if [[ -z "$conda_env" ]]; then
        echo "--conda-env is required for --mode conda" >&2
        exit 2
      fi
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      # Default for the benchmark chain: prefer report python_path when present.
      report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
      if [[ -f "$report_path" ]]; then
        report_py="$(
          "$util_python" - <<'PY' "$report_path" 2>/dev/null || true
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("python_path",""))
except Exception:
  print("")
PY
        )"
        if [[ -n "$report_py" ]]; then
          py_cmd=("$report_py")
          python_resolution="report.json"
        else
          py_cmd=("$util_python")
          python_resolution="python(PATH)"
        fi
      else
        py_cmd=("$util_python")
        python_resolution="python(PATH)"
      fi
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

python_exec="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_version="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

framework="unknown"
if "${py_cmd[@]}" -c 'import torch' >/dev/null 2>&1; then
  framework="pytorch"
fi

failure_category="unknown"
status="success"
exit_code=0

project_args=()
targets=()
decision_reason=""

# Ensure required outputs exist even if we fail early.
echo '{"generalDiagnostics":[]}' > "$out_json"

if [[ -f pyrightconfig.json ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="pyrightconfig.json found -> --project pyrightconfig.json"
elif [[ -f pyproject.toml ]] && grep -qF '[tool.pyright]' pyproject.toml >/dev/null 2>&1; then
  project_args=(--project pyproject.toml)
  decision_reason="pyproject.toml with [tool.pyright] found -> --project pyproject.toml"
elif [[ -d src ]] && find src -type f -name '*.py' -print -quit 2>/dev/null | grep -q .; then
  targets=(src)
  if [[ -d tests ]] && find tests -type f -name '*.py' -print -quit 2>/dev/null | grep -q .; then
    targets+=(tests)
  fi
  decision_reason="src/ layout detected -> targets=src (+tests if present)"
else
  pkg_dirs=()
  while IFS= read -r -d '' init_file; do
    rel="${init_file#./}"
    top="${rel%%/*}"
    case "$top" in
      .git|.venv|venv|build|dist|node_modules|build_output|benchmark_assets|benchmark_scripts) continue ;;
    esac
    pkg_dirs+=("$top")
  done < <(find . -maxdepth 3 -type f -name '__init__.py' -print0 2>/dev/null || true)
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    mapfile -t pkg_dirs < <(printf '%s\n' "${pkg_dirs[@]}" | sort -u)
  fi
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
    decision_reason="Detected package dirs with __init__.py -> targets=${targets[*]}"
  fi
fi

if [[ ${#project_args[@]} -eq 0 ]] && [[ ${#targets[@]} -eq 0 ]]; then
  status="failure"
  exit_code=1
  failure_category="entrypoint_not_found"
  echo '{"generalDiagnostics":[]}' > "$out_json"
  "$util_python" - <<'PY' "$analysis_json" "$results_json" "$repo" "$decision_reason" "$python_exec" "$python_version" "$git_commit" "$framework" "$status" "$exit_code" "$failure_category" "$timeout_sec" "$python_resolution"
import json, sys
analysis_json, results_json = sys.argv[1], sys.argv[2]
repo, decision_reason = sys.argv[3], sys.argv[4]
python_exec, python_version, git_commit, framework = sys.argv[5], sys.argv[6], sys.argv[7], sys.argv[8]
status, exit_code, failure_category = sys.argv[9], int(sys.argv[10]), sys.argv[11]
timeout_sec, python_resolution = int(sys.argv[12]), sys.argv[13]

analysis = {
  "missing_packages": [],
  "pyright": {"generalDiagnostics": []},
  "meta": {
    "repo": repo,
    "decision_reason": decision_reason,
    "python_cmd": python_exec,
    "python_version": python_version,
    "git_commit": git_commit,
    "framework": framework,
    "python_resolution": python_resolution,
    "files_scanned": 0,
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
  },
}
with open(analysis_json, "w", encoding="utf-8") as f:
  json.dump(analysis, f, ensure_ascii=False, indent=2)

results = {
  "status": status,
  "skip_reason": "unknown",
  "exit_code": exit_code,
  "stage": "pyright",
  "task": "check",
  "command": "pyright (auto-detect targets) - failed to find targets",
  "timeout_sec": timeout_sec,
  "framework": framework,
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": analysis["meta"],
  "failure_category": failure_category,
  "error_excerpt": "",
  **analysis["metrics"],
}
with open(results_json, "w", encoding="utf-8") as f:
  json.dump(results, f, ensure_ascii=False, indent=2)
PY
  exit 1
fi

if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_cmd="$python_exec -m pip install -q pyright"
  echo "pyright not found; attempting install: $install_cmd"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pip_rc=$?
  set -e
  if [[ $pip_rc -ne 0 ]]; then
    status="failure"
    exit_code=1
    if grep -qiE "(connection|timed out|Temporary failure|Name or service not known)" "$log_path" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
  fi
fi

pyright_rc=0
cmd_str=()
cmd_str+=("${py_cmd[@]}" -m pyright)
if [[ ${#targets[@]} -gt 0 ]]; then
  cmd_str+=("${targets[@]}")
else
  cmd_str+=(".")
fi
cmd_str+=(--level "$pyright_level" --outputjson)
if [[ ${#project_args[@]} -gt 0 ]]; then
  cmd_str+=("${project_args[@]}")
fi
if [[ ${#pyright_extra_args[@]} -gt 0 ]]; then
  cmd_str+=("${pyright_extra_args[@]}")
fi

echo "Running: ${cmd_str[*]}"
set +e
timeout_cmd=()
if command -v timeout >/dev/null 2>&1; then
  timeout_cmd=(timeout "${timeout_sec}s")
fi
("${timeout_cmd[@]}" "${cmd_str[@]}" > "$out_json") 2>>"$log_path"
pyright_rc=$?
set -e
if [[ $pyright_rc -eq 124 ]]; then
  status="failure"
  exit_code=1
  failure_category="timeout"
fi

"$util_python" - <<'PY' \
  "$out_json" "$analysis_json" "$results_json" "$repo" "$decision_reason" \
  "$python_exec" "$python_version" "$git_commit" "$framework" "$timeout_sec" \
  "$status" "$exit_code" "$failure_category" "$pyright_rc" "$install_attempted" "$install_cmd" \
  "${targets[*]}" "${project_args[*]}" "$python_resolution"
import ast
import json
import os
import pathlib
import re
import sys
from typing import Iterable

out_json = pathlib.Path(sys.argv[1])
analysis_json = pathlib.Path(sys.argv[2])
results_json = pathlib.Path(sys.argv[3])
repo = sys.argv[4]
decision_reason = sys.argv[5]
python_exec = sys.argv[6]
python_version = sys.argv[7]
git_commit = sys.argv[8]
framework = sys.argv[9]
timeout_sec = int(sys.argv[10])
status = sys.argv[11]
exit_code = int(sys.argv[12])
failure_category = sys.argv[13]
pyright_rc = int(sys.argv[14])
install_attempted = bool(int(sys.argv[15]))
install_cmd = sys.argv[16]
targets_str = sys.argv[17]
project_args_str = sys.argv[18]
python_resolution = sys.argv[19]

repo_root = pathlib.Path(repo).resolve()

raw = {}
parse_err = None
try:
    raw = json.loads(out_json.read_text(encoding="utf-8", errors="replace"))
except Exception as e:
    parse_err = f"{type(e).__name__}: {e}"
    raw = {"generalDiagnostics": []}

diagnostics = raw.get("generalDiagnostics", []) or []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
)

def iter_py_files(roots: Iterable[pathlib.Path]) -> Iterable[pathlib.Path]:
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
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
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

targets = [t for t in targets_str.split() if t]
roots = [repo_root / t for t in targets] if targets else [repo_root]

all_imported_packages = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

analysis = {
    "missing_packages": missing_packages,
    "pyright": raw,
    "meta": {
        "repo": repo,
        "decision_reason": decision_reason,
        "python_cmd": python_exec,
        "python_version": python_version,
        "git_commit": git_commit,
        "framework": framework,
        "python_resolution": python_resolution,
        "targets": targets,
        "project_args": project_args_str.split(),
        "pyright_exit_code": pyright_rc,
        "pyright_install_attempted": install_attempted,
        "pyright_install_command": install_cmd,
        "pyright_json_parse_error": parse_err,
        "files_scanned": files_scanned,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

# Include required stage fields + metrics for summarization.
results = {
    "status": status if status != "success" else ("success" if exit_code == 0 else "failure"),
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "pyright",
    "task": "check",
    "command": analysis["meta"].get("python_cmd", "") + " -m pyright ...",
    "timeout_sec": timeout_sec,
    "framework": framework,
    "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
    "meta": analysis["meta"],
    "failure_category": failure_category if exit_code != 0 else "unknown",
    "error_excerpt": "",
    **analysis["metrics"],
}
results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
PY

if [[ "$status" == "failure" ]] || [[ "$exit_code" -ne 0 ]]; then
  # Include tail of log for error_excerpt.
  tail_excerpt="$(tail -n 220 "$log_path" 2>/dev/null || true)"
  "$util_python" - <<'PY' "$results_json" "$tail_excerpt"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
tail=sys.argv[2]
try:
  d=json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(0)
d["error_excerpt"]=tail
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
fi

exit "$exit_code"
