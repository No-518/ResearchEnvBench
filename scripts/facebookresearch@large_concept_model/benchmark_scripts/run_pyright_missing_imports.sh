#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

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
  --out-root <path>              Default: build_output
  --level <error|warning|...>    Default: error
  --install-pyright              Attempt to install pyright into the selected environment if missing
  -- <pyright args...>           Extra args passed to Pyright (e.g. --project pyrightconfig.json)

Outputs:
  <out-root>/pyright/log.txt
  <out-root>/pyright/pyright_output.json
  <out-root>/pyright/analysis.json
  <out-root>/pyright/results.json
EOF
}

mode="system"
repo=""
out_root="build_output"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
install_pyright=0
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-root) out_root="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --venv) venv_dir="${2:-}"; shift 2 ;;
    --conda-env) conda_env="${2:-}"; shift 2 ;;
    --install-pyright) install_pyright=1; shift ;; # kept for compatibility; install is attempted regardless
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
stage_dir="$repo/$out_root/pyright"
mkdir -p "$stage_dir"

log_txt="$stage_dir/log.txt"
out_json="$stage_dir/pyright_output.json"
analysis_json="$stage_dir/analysis.json"
results_json="$stage_dir/results.json"

exec > >(tee "$log_txt") 2>&1

command_str="bash benchmark_scripts/run_pyright_missing_imports.sh --repo $repo --out-root $out_root --level $pyright_level"

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

python_ok=0
python_executable=""
python_version=""
if python_executable="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null)"; then
  python_ok=1
  python_version="$("${py_cmd[@]}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
fi

attempted_install=0
install_command=""
install_rc=0

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"

if [[ "$python_ok" -ne 1 ]]; then
  echo "[pyright] ERROR: failed to run python via: ${py_cmd[*]}"
  failure_category="deps"
else
  if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
    echo "[pyright] pyright not importable in selected environment."
    attempted_install=1
    install_command="$("${py_cmd[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || true) -m pip install -q pyright"
    set +e
    "${py_cmd[@]}" -m pip install -q pyright
    install_rc=$?
    set -e
    if [[ "$install_rc" -ne 0 ]]; then
      echo "[pyright] ERROR: failed to install pyright (rc=$install_rc)."
      failure_category="deps"
    fi
  fi
fi

# Determine Pyright targets/project automatically.
project_arg=()
targets=()

cd "$repo"

if [[ -f "pyrightconfig.json" ]]; then
  project_arg=(--project pyrightconfig.json)
elif [[ -f "pyproject.toml" ]] && python3 - <<'PY' >/dev/null 2>&1; then
import re
from pathlib import Path
text = Path("pyproject.toml").read_text(encoding="utf-8", errors="ignore")
raise SystemExit(0 if re.search(r"^\\[tool\\.pyright\\]\\s*$", text, re.M) else 1)
PY
  project_arg=(--project pyproject.toml)
else
  if [[ -d "src" ]]; then
    targets+=(src)
    [[ -d "tests" ]] && targets+=(tests)
  else
    mapfile -t init_dirs < <(find . -type f -name "__init__.py" \
      -not -path "./.git/*" \
      -not -path "./.venv/*" \
      -not -path "./venv/*" \
      -not -path "./build/*" \
      -not -path "./dist/*" \
      -not -path "./node_modules/*" \
      -not -path "./build_output/*" \
      -not -path "./benchmark_assets/*" \
      -not -path "./benchmark_scripts/*" \
      2>/dev/null | sed 's|/\\{0,1\\}__init__\\.py$||' | sed 's|^\\./||' | sort -u)

    # Reduce to top-level package directories (remove nested paths).
    if [[ "${#init_dirs[@]}" -gt 0 ]]; then
      for d in "${init_dirs[@]}"; do
        [[ -z "$d" ]] && continue
        skip=0
        for existing in "${targets[@]:-}"; do
          if [[ "$d" == "$existing"/* ]]; then
            skip=1
            break
          fi
        done
        if [[ "$skip" -eq 0 ]]; then
          targets+=("$d")
        fi
      done
    fi
  fi
fi

if [[ "${#project_arg[@]}" -eq 0 && "${#targets[@]}" -eq 0 ]]; then
  echo "[pyright] ERROR: could not determine Python targets (no pyrightconfig, no tool.pyright, no src/, no packages with __init__.py)."
  failure_category="entrypoint_not_found"
else
  # Run Pyright (always produce JSON output even if exit code is non-zero).
  if [[ "$python_ok" -eq 1 && "$install_rc" -eq 0 ]]; then
    pyright_cmd=("${py_cmd[@]}" -m pyright)
    if [[ "${#project_arg[@]}" -gt 0 ]]; then
      pyright_cmd+=("${project_arg[@]}")
    else
      pyright_cmd+=("${targets[@]}")
    fi
    pyright_cmd+=(--level "$pyright_level" --outputjson "${pyright_extra_args[@]}")

    echo "[pyright] running: ${pyright_cmd[*]}"
    set +e
    "${pyright_cmd[@]}" >"$out_json"
    pyright_rc=$?
    set -e
    echo "[pyright] pyright exit code (ignored for stage success): $pyright_rc"
    command_str="${pyright_cmd[*]}"

    if [[ ! -s "$out_json" ]]; then
      echo '{"generalDiagnostics":[],"summary":{}}' >"$out_json"
    fi

    # Analyze missing imports and compute metrics.
    OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" MODE="$mode" PY_CMD="${py_cmd[*]}" \
      ATTEMPTED_INSTALL="$attempted_install" INSTALL_COMMAND="$install_command" INSTALL_RC="$install_rc" \
      "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])

repo_root = pathlib.Path(".").resolve()

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {"generalDiagnostics": [], "summary": {}}

diagnostics = data.get("generalDiagnostics", []) or []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import \"([^.\"\\s]+)')
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

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "files_scanned": files_scanned,
        "attempted_install": bool(int(os.environ.get("ATTEMPTED_INSTALL", "0"))),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "install_rc": int(os.environ.get("INSTALL_RC", "0") or 0),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
results_json.write_text(json.dumps(payload["metrics"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(missing_package_ratio)
PY

    status="success"
    exit_code=0
    failure_category="unknown"
  else
    # Ensure mandatory output files exist.
    [[ -f "$out_json" ]] || echo '{"generalDiagnostics":[],"summary":{}}' >"$out_json"
    [[ -f "$analysis_json" ]] || echo '{}' >"$analysis_json"
    [[ -f "$results_json" ]] || echo '{}' >"$results_json"
    status="failure"
    exit_code=1
    [[ "$failure_category" == "unknown" ]] && failure_category="deps"
  fi
fi

# Ensure mandatory outputs exist even on early failure paths.
[[ -f "$out_json" ]] || echo '{"generalDiagnostics":[],"summary":{}}' >"$out_json"
[[ -f "$analysis_json" ]] || echo '{}' >"$analysis_json"

# Build stage results.json (must include required fields).
git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
error_excerpt="$(tail -n 220 "$log_txt" || true)"

metrics_json="$results_json"
missing_packages_count="$(python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
try:
    d = json.loads(Path(${metrics_json@Q}).read_text(encoding="utf-8"))
    print(d.get("missing_packages_count",""))
except Exception:
    print("")
PY
)"

total_imported_packages_count="$(python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
try:
    d = json.loads(Path(${metrics_json@Q}).read_text(encoding="utf-8"))
    print(d.get("total_imported_packages_count",""))
except Exception:
    print("")
PY
)"

missing_package_ratio="$(python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
try:
    d = json.loads(Path(${metrics_json@Q}).read_text(encoding="utf-8"))
    print(d.get("missing_package_ratio",""))
except Exception:
    print("")
PY
)"

RESULTS_JSON_PATH="$results_json" \
  STATUS="$status" \
  EXIT_CODE="$exit_code" \
  COMMAND_STR="$command_str" \
  PYTHON_EXECUTABLE="$python_executable" \
  PYTHON_VERSION="$python_version" \
  GIT_COMMIT="$git_commit" \
  ATTEMPTED_INSTALL="$attempted_install" \
  INSTALL_COMMAND="$install_command" \
  INSTALL_RC="$install_rc" \
  MISSING_PACKAGES_COUNT="${missing_packages_count:-0}" \
  TOTAL_IMPORTED_PACKAGES_COUNT="${total_imported_packages_count:-0}" \
  MISSING_PACKAGE_RATIO="$missing_package_ratio" \
  FAILURE_CATEGORY="$failure_category" \
  LOG_PATH="$log_txt" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

def tail_file(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]) if len(lines) > max_lines else "\n".join(lines)

results_path = Path(os.environ["RESULTS_JSON_PATH"])
status = os.environ.get("STATUS", "failure")
exit_code = int(os.environ.get("EXIT_CODE", "1") or 1)

payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": 0 if status == "success" or status == "skipped" else 1,
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_EXECUTABLE", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {},
        "decision_reason": "Run Pyright on the most likely project targets and report only reportMissingImports diagnostics.",
        "pyright_install_attempted": int(os.environ.get("ATTEMPTED_INSTALL", "0") or 0),
        "pyright_install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_install_rc": int(os.environ.get("INSTALL_RC", "0") or 0),
    },
    "metrics": {
        "missing_packages_count": int(os.environ.get("MISSING_PACKAGES_COUNT", "0") or 0),
        "total_imported_packages_count": int(os.environ.get("TOTAL_IMPORTED_PACKAGES_COUNT", "0") or 0),
        "missing_package_ratio": os.environ.get("MISSING_PACKAGE_RATIO", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail_file(Path(os.environ.get("LOG_PATH", ""))),
}

# Preserve the script's exit_code semantics.
payload["exit_code"] = exit_code

results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

exit "$exit_code"
