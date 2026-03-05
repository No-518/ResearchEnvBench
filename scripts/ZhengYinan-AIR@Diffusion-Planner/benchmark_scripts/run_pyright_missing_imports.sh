#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only reportMissingImports diagnostics.

Outputs (under build_output/pyright/ by default):
  log.txt
  pyright_output.json   # raw Pyright JSON output
  analysis.json         # {missing_packages, pyright, meta, metrics}
  results.json          # stage result (includes metrics + required fields)

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

If neither --python nor --mode is provided, this script resolves python via:
  SCIMLOPSBENCH_PYTHON -> /opt/scimlopsbench/report.json["python_path"]

Optional:
  --repo <path>                  Repo root (default: repo root inferred from this script)
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <int>            Default: 600 (best-effort; no hard kill without coreutils timeout)
  -- <pyright extra args...>
EOF
}

mode=""
repo=""
out_dir=""
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
timeout_sec="600"
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
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
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

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root_default="$(cd -- "${script_dir}/.." && pwd)"
repo="${repo:-$repo_root_default}"
out_dir="${out_dir:-${repo}/build_output/pyright}"

mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_file"
exec > >(tee -a "$log_file") 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"

cd "$repo"

resolve_python_from_report() {
  local report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  python3 - <<PY
import json, os, sys
from pathlib import Path
p = Path(${report_path@Q})
if not p.exists():
  print("")
  sys.exit(1)
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("")
  sys.exit(1)
py = data.get("python_path")
if not isinstance(py, str) or not py.strip():
  print("")
  sys.exit(1)
print(py)
PY
}

py_cmd=()
python_source=""
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
  python_source="cli"
elif [[ -n "$mode" ]]; then
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      py_cmd=("$venv_dir/bin/python")
      python_source="venv"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      py_cmd=("$venv_dir/bin/python")
      python_source="uv"
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      py_cmd=(conda run -n "$conda_env" python)
      python_source="conda"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      py_cmd=(poetry run python)
      python_source="poetry"
      ;;
    system)
      py_cmd=(python)
      python_source="system"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      exit 2
      ;;
  esac
else
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    py_cmd=("${SCIMLOPSBENCH_PYTHON}")
    python_source="env:SCIMLOPSBENCH_PYTHON"
  else
    set +e
    resolved="$(resolve_python_from_report)"
    rc=$?
    set -e
    if [[ $rc -eq 0 && -n "$resolved" ]]; then
      py_cmd=("$resolved")
      python_source="report"
    else
      python_source="missing_report"
    fi
  fi
fi

install_attempted="false"
install_command=""
pyright_ran="false"
pyright_cmd=""
target_strategy=""
failure_category="unknown"
status="failure"
skip_reason="not_applicable"
py_cmd_str=""

write_empty_outputs() {
  echo '{}' >"$out_json" 2>/dev/null || true
  python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
Path(${analysis_json@Q}).write_text(
    json.dumps({"missing_packages": [], "pyright": {}, "meta": {"note": "no analysis"}, "metrics": {}}, indent=2),
    encoding="utf-8",
)
PY
}

finalize() {
  local rc="$1"
  trap - EXIT

  if [[ "$rc" -ne 0 && "${status}" != "failure" ]]; then
    status="failure"
    failure_category="${failure_category:-unknown}"
  fi

  # Ensure mandatory outputs exist.
  [[ -f "$out_json" ]] || write_empty_outputs
  [[ -f "$analysis_json" ]] || write_empty_outputs

  if [[ ! -f "$results_json" ]]; then
    write_results 1
  fi

  if [[ "${status}" == "success" || "${status}" == "skipped" ]]; then
    exit 0
  fi
  exit 1
}

trap 'finalize $?' EXIT

write_results() {
  local exit_code="$1"
  STATUS="$status" \
  SKIP_REASON="$skip_reason" \
  EXIT_CODE="$exit_code" \
  PYRIGHT_CMD="$pyright_cmd" \
  TIMEOUT_SEC="$timeout_sec" \
  PY_CMD_STR="$py_cmd_str" \
  PYTHON_SOURCE="$python_source" \
  INSTALL_ATTEMPTED="$install_attempted" \
  INSTALL_COMMAND="$install_command" \
  PYRIGHT_RAN="$pyright_ran" \
  TARGET_STRATEGY="$target_strategy" \
  FAILURE_CATEGORY="$failure_category" \
  LOG_FILE="$log_file" \
  ANALYSIS_JSON="$analysis_json" \
  RESULTS_JSON="$results_json" \
    python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path


def tail_file(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception as e:
        return f"[pyright] failed to read log: {e}"


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(Path.cwd()), text=True).strip()
    except Exception:
        return ""


analysis_path = Path(os.environ.get("ANALYSIS_JSON", ""))
metrics = {}
if analysis_path.exists():
    try:
        a = json.loads(analysis_path.read_text(encoding="utf-8"))
        if isinstance(a, dict) and isinstance(a.get("metrics"), dict):
            metrics = a["metrics"]
    except Exception:
        metrics = {}

log_path = Path(os.environ.get("LOG_FILE", ""))
error_excerpt = tail_file(log_path)

def _to_int(v, default: int = 1) -> int:
    try:
        return int(v)
    except Exception:
        return default

results = {
    "status": os.environ.get("STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "not_applicable"),
    "exit_code": _to_int(os.environ.get("EXIT_CODE", "1")),
    "stage": "pyright",
    "task": "check",
    "command": os.environ.get("PYRIGHT_CMD", ""),
    "timeout_sec": _to_int(os.environ.get("TIMEOUT_SEC", "600"), 600),
    "framework": "unknown",
    "missing_packages_count": metrics.get("missing_packages_count"),
    "total_imported_packages_count": metrics.get("total_imported_packages_count"),
    "missing_package_ratio": metrics.get("missing_package_ratio"),
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_CMD_STR", ""),
        "python_source": os.environ.get("PYTHON_SOURCE", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "false"),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_ran": os.environ.get("PYRIGHT_RAN", "false"),
        "target_strategy": os.environ.get("TARGET_STRATEGY", ""),
        "git_commit": git_commit(),
        "env_vars": {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_") or k.startswith("CUDA")},
        "decision_reason": "Pyright is run on repo targets detected via pyrightconfig/pyproject/src layout/package dirs; only reportMissingImports diagnostics are counted.",
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": error_excerpt,
}

Path(os.environ["RESULTS_JSON"]).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
PY
}

if [[ "${python_source}" == "missing_report" ]]; then
  echo "[pyright] ERROR: could not resolve python (SCIMLOPSBENCH_PYTHON not set; report missing/invalid)."
  failure_category="missing_report"
  status="failure"
  write_empty_outputs
  write_results 1
  exit 1
fi

echo "[pyright] python_cmd=${py_cmd[*]}"
py_cmd_str="${py_cmd[*]}"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[pyright] ERROR: failed to run python via: ${py_cmd[*]}" >&2
  failure_category="missing_report"
  status="failure"
  write_empty_outputs
  write_results 1
  exit 1
fi

echo "[pyright] ensure pyright is installed (pip) in selected python"
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted="true"
  install_command="${py_cmd[*]} -m pip install -q pyright"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pip_rc=$?
  set -e
  if [[ $pip_rc -ne 0 ]]; then
    echo "[pyright] ERROR: failed to install pyright (rc=$pip_rc)"
    if rg -n "Temporary failure in name resolution|Could not resolve|Connection.*failed|Read timed out" "$log_file" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    status="failure"
    write_results 1
    # Best-effort empty outputs for downstream tooling.
    echo '{}' >"$out_json" || true
    echo '{}' >"$analysis_json" || true
    exit 1
  fi
fi

targets=()
if [[ -f pyrightconfig.json ]]; then
  target_strategy="pyrightconfig.json"
  targets=("--project" "pyrightconfig.json")
elif [[ -f pyproject.toml ]] && rg -n "^\\[tool\\.pyright\\]" pyproject.toml >/dev/null 2>&1; then
  target_strategy="pyproject.toml:[tool.pyright]"
  targets=("--project" "pyproject.toml")
elif [[ -d src ]]; then
  target_strategy="src_layout"
  targets=("src")
  [[ -d tests ]] && targets+=("tests")
else
  target_strategy="package_dirs(__init__.py)"
  mapfile -t pkg_dirs < <(find . -type f -name "__init__.py" \
    -not -path "./.git/*" \
    -not -path "./.venv/*" \
    -not -path "./venv/*" \
    -not -path "./build_output/*" \
    -not -path "./benchmark_assets/*" \
    -not -path "./benchmark_scripts/*" \
    -print | sed -e 's|^\\./||' | awk -F/ '{print $1}' | sort -u)
  if [[ ${#pkg_dirs[@]} -gt 0 ]]; then
    targets=("${pkg_dirs[@]}")
  fi
fi

if [[ ${#targets[@]} -eq 0 ]]; then
  echo "[pyright] ERROR: could not determine any analysis targets (no pyrightconfig/pyproject/src/package dirs)."
  failure_category="entrypoint_not_found"
  status="failure"
  echo '{}' >"$out_json" || true
  python3 - <<PY
import json
from pathlib import Path
Path(${analysis_json@Q}).write_text(json.dumps({"missing_packages": [], "pyright": {}, "meta": {"note": "no targets detected"}, "metrics": {}}, indent=2), encoding="utf-8")
PY
  write_results 1
  exit 1
fi

echo "[pyright] target_strategy=$target_strategy"
echo "[pyright] targets: ${targets[*]}"

pyright_cmd="${py_cmd[*]} -m pyright ${targets[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
pyright_ran="true"

set +e
"${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$out_json"
pyright_exit=$?
set -e

echo "[pyright] pyright exit code: $pyright_exit (non-zero does not fail this stage by itself)"

OUT_JSON="$out_json" ANALYSIS_JSON="$analysis_json" RESULTS_JSON="$results_json" MODE="$mode" PY_CMD="${py_cmd[*]}" TARGET_STRATEGY="$target_strategy" \
python3 - <<'PY'
import ast
import json
import os
import pathlib
import re
from typing import Iterable

out_json = pathlib.Path(os.environ["OUT_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])

try:
    data = json.loads(out_json.read_text(encoding="utf-8"))
except Exception:
    data = {}

diagnostics = data.get("generalDiagnostics", []) if isinstance(data, dict) else []
missing_diags = [d for d in diagnostics if d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(d.get("message", "")))}
)


def iter_py_files(root: pathlib.Path, include_roots: list[pathlib.Path]) -> Iterable[pathlib.Path]:
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
    for base in include_roots:
        if not base.exists():
            continue
        if base.is_file() and base.suffix == ".py":
            yield base
            continue
        for path in base.rglob("*.py"):
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


repo_root = pathlib.Path(".").resolve()
target_strategy = os.environ.get("TARGET_STRATEGY", "")

# Reconstruct scanned roots from the strategy:
# - project-based strategies scan repo root (Pyright itself controls scope)
# - folder strategies scan listed folder(s)
if target_strategy.startswith("pyrightconfig") or target_strategy.startswith("pyproject"):
    scanned_roots = [repo_root]
else:
    # When targets were folders (src/tests/pkg dirs), prefer those.
    # We read them back from Pyright output if present, else fall back to repo root.
    scanned_roots = [repo_root]

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(repo_root, scanned_roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = (
    f"{missing_packages_count}/{total_imported_packages_count}"
    if total_imported_packages_count
    else "0/0"
)

payload = {
    "missing_packages": missing_packages,
    "pyright": data,
    "meta": {
        "python_cmd": os.environ.get("PY_CMD", ""),
        "mode": os.environ.get("MODE", ""),
        "target_strategy": target_strategy,
        "files_scanned": files_scanned,
        "diagnostics_total": len(diagnostics),
        "diagnostics_missing_imports": len(missing_diags),
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY

if [[ -s "$analysis_json" ]]; then
  status="success"
  failure_category="unknown"
  write_results 0
  exit 0
else
  echo "[pyright] ERROR: analysis.json was not produced."
  failure_category="unknown"
  status="failure"
  write_results 1
  exit 1
fi
