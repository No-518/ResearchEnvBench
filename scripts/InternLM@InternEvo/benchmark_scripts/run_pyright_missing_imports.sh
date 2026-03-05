#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright in an already-configured environment and report only missing-import diagnostics.

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
  --mode system                  Use: python from PATH

Optional:
  --level <error|warning|...>    Default: error
  --out-dir <path>               Default: build_output/pyright
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonpath .)

Notes:
  - If `import pyright` fails, this script attempts: "<python> -m pip install -q pyright"
  - Pyright non-zero exit will not crash this script; results are still produced.
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

if [[ -z "$repo" ]]; then
  echo "--repo is required" >&2
  usage
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$repo"

mkdir -p "$out_dir"
log_txt="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_txt"
exec > >(tee -a "$log_txt") 2>&1

stage_status="failure"
stage_exit_code=1
failure_category="unknown"
skip_reason="not_applicable"
command_str=""
install_attempted=0
install_cmd=""
install_rc=""
decision_reason="Auto-selected Pyright project/targets per benchmark priority rules."

have_rg=0
if command -v rg >/dev/null 2>&1; then
  have_rg=1
fi

file_has_re() {
  local pattern="$1"
  local file="$2"
  if [[ "$have_rg" -eq 1 ]]; then
    rg -n "$pattern" "$file" >/dev/null 2>&1
  else
    grep -E -n "$pattern" "$file" >/dev/null 2>&1
  fi
}

quote_cmd() {
  local out=""
  local arg
  for arg in "$@"; do
    out+=$(printf '%q ' "$arg")
  done
  printf '%s' "${out% }"
}

py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
else
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      ;;
    system)
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

echo "[pyright] repo: $repo"
echo "[pyright] out_dir: $out_dir"
echo "[pyright] python cmd: ${py_cmd[*]}"

python_exe="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_ver="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
if [[ -z "$python_exe" ]]; then
  failure_category="deps"
  stage_status="failure"
  stage_exit_code=1
  echo "[pyright] failed to run python via: ${py_cmd[*]}" >&2
  echo "{}" >"$out_json"
  cat >"$analysis_json" <<'JSON'
{
  "missing_packages": [],
  "pyright": {},
  "meta": {"note": "python execution failed; analysis unavailable"},
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0",
    "missing_packages": []
  }
}
JSON
else
  export PIP_CACHE_DIR="$REPO_ROOT/benchmark_assets/cache/pip"
  mkdir -p "$PIP_CACHE_DIR"

  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
    install_attempted=1
    install_cmd="${py_cmd[*]} -m pip install -q pyright"
    echo "[pyright] pyright not found; attempting install: $install_cmd"
    set +e
    "${py_cmd[@]}" -m pip install -q pyright
    install_rc="$?"
    set -e
    if [[ "$install_rc" != "0" ]]; then
      echo "[pyright] pyright install failed (rc=$install_rc)" >&2
      if file_has_re "Temporary failure|Name or service not known|Connection timed out|TLS|SSL|No matching distribution|Could not fetch|Could not find a version" "$log_txt"; then
        failure_category="download_failed"
      else
        failure_category="deps"
      fi
      stage_status="failure"
      stage_exit_code=1
      echo "{}" >"$out_json"
    fi
  fi

  if [[ "$stage_status" != "failure" ]]; then
    :
  else
    # keep going to still attempt to select targets & emit analysis/results
    :
  fi

  # Target selection (priority rules).
  pyright_args=()
  targets=()
  if [[ -f "pyrightconfig.json" ]]; then
    pyright_args+=(--project "pyrightconfig.json")
    decision_reason="Found pyrightconfig.json; using --project pyrightconfig.json."
  elif [[ -f "pyproject.toml" ]] && file_has_re "^\\[tool\\.pyright\\]" "pyproject.toml"; then
    pyright_args+=(--project "pyproject.toml")
    decision_reason="Found [tool.pyright] in pyproject.toml; using --project pyproject.toml."
  elif [[ -d "src" ]]; then
    targets+=("src")
    [[ -d "tests" ]] && targets+=("tests")
    decision_reason="No pyright config found; using src/ (and tests/ if present)."
  else
    # Detect package dirs containing __init__.py.
    mapfile -t pkg_dirs < <(
      "${py_cmd[@]}" - <<'PY'
import os
import pathlib

root = pathlib.Path(".").resolve()
exclude = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "build", "dist", "node_modules", ".venv", "venv", "benchmark_assets", "build_output"}
pkgs = set()
for init in root.rglob("__init__.py"):
    parts = init.parts
    if any(p in exclude for p in parts):
        continue
    parent = init.parent
    try:
        rel = parent.relative_to(root)
    except Exception:
        continue
    # Prefer top-level package directories.
    top = rel.parts[0] if rel.parts else ""
    if top and top not in exclude:
        pkgs.add(top)
print("\n".join(sorted(pkgs)))
PY
    )
    if [[ "${#pkg_dirs[@]}" -gt 0 ]]; then
      targets+=("${pkg_dirs[@]}")
      decision_reason="No pyright config/src found; using detected package dirs with __init__.py."
    else
      failure_category="entrypoint_not_found"
      stage_status="failure"
      stage_exit_code=1
      decision_reason="No pyright config, no src/, and no package dirs with __init__.py detected."
      echo "{}" >"$out_json"
    fi
  fi

  if [[ "$failure_category" != "entrypoint_not_found" && "$failure_category" != "deps" && "$failure_category" != "download_failed" ]]; then
    failure_category="not_applicable"
  fi

  # Run pyright only if we have a python that can import pyright and we found a target/project.
  if "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1 && [[ "$failure_category" != "entrypoint_not_found" ]]; then
    cmd_argv=("${py_cmd[@]}" -m pyright "${pyright_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" "${targets[@]}")
    command_str="$(quote_cmd "${cmd_argv[@]}") > $(printf '%q' "$out_json")"
    echo "[pyright] command: $command_str"
    set +e
    "${cmd_argv[@]}" >"$out_json"
    prc="$?"
    set -e
    echo "[pyright] pyright exit code (ignored): $prc"
  fi

  # Build analysis/results JSON (always).
  PYRIGHT_OUT="$out_json" ANALYSIS_JSON="$analysis_json" \
    MODE="$mode" PY_CMD="${py_cmd[*]}" PY_EXE="$python_exe" PY_VER="$python_ver" \
    INSTALL_ATTEMPTED="$install_attempted" INSTALL_CMD="$install_cmd" INSTALL_RC="$install_rc" \
    DECISION_REASON="$decision_reason" \
    "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["PYRIGHT_OUT"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
repo_root = pathlib.Path(".").resolve()

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if isinstance(d, dict) and (m := pattern.search(d.get("message", "")))}
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
        "benchmark_assets",
        "build_output",
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

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "python_executable": os.environ.get("PY_EXE", ""),
        "python_version": os.environ.get("PY_VER", ""),
        "files_scanned": files_scanned,
        "pyright_install_attempted": os.environ.get("INSTALL_ATTEMPTED", "0") == "1",
        "pyright_install_cmd": os.environ.get("INSTALL_CMD", ""),
        "pyright_install_rc": os.environ.get("INSTALL_RC", ""),
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
        "missing_packages": missing_packages,
    },
}

analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

  # Stage status: if failure_category indicates hard failure, keep failure; else success.
  if [[ "$failure_category" == "not_applicable" ]]; then
    stage_status="success"
    stage_exit_code=0
  else
    stage_status="failure"
    stage_exit_code=1
  fi
fi

# Final results.json (stage envelope) always written.
git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

sys_py="${python_exe:-python}"
PY_LOG_TXT="$log_txt" "$sys_py" - <<PY
import json, os, pathlib
log_txt = pathlib.Path(os.environ.get("PY_LOG_TXT", ""))
try:
  error_excerpt = "\\n".join(log_txt.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
except Exception:
  error_excerpt = ""
analysis = {}
try:
  analysis = json.loads(pathlib.Path(${analysis_json@Q}).read_text(encoding="utf-8"))
except Exception:
  analysis = {}
metrics = analysis.get("metrics", {}) if isinstance(analysis, dict) else {}
missing_packages = analysis.get("missing_packages", []) if isinstance(analysis, dict) else []
out = {
  "status": "${stage_status}",
  "skip_reason": "${skip_reason}",
  "exit_code": ${stage_exit_code},
  "stage": "pyright",
  "task": "check",
  "command": ${command_str@Q},
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {
    "python": ${python_exe@Q} + (" (" + ${python_ver@Q} + ")" if ${python_ver@Q} else ""),
    "git_commit": ${git_commit@Q},
    "env_vars": {k: os.environ.get(k, "") for k in [
      "PIP_CACHE_DIR","SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES","HF_HOME","TRANSFORMERS_CACHE","HF_DATASETS_CACHE","TORCH_HOME"
    ] if os.environ.get(k) is not None},
    "decision_reason": ${decision_reason@Q},
    "pyright": {
      "install_attempted": bool(int("${install_attempted}")),
      "install_cmd": ${install_cmd@Q},
      "install_rc": ${install_rc@Q},
      "mode": ${mode@Q},
      "py_cmd": ${py_cmd[*]@Q},
    },
  },
  "failure_category": ${failure_category@Q},
  "error_excerpt": error_excerpt,
  "metrics": metrics,
  "missing_packages": missing_packages,
}
open("${results_json}", "w", encoding="utf-8").write(json.dumps(out, indent=2, ensure_ascii=False) + "\\n")
PY

exit "$stage_exit_code"
