#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright and report only missing-import diagnostics.

Outputs (always written):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Use: conda run -n <n> python
  --mode poetry                  Use: poetry run python
  --mode system                  Use: python from PATH

Optional:
  --repo <path>                  Repo root (default: current directory)
  --out-dir <path>               Output dir (default: build_output/pyright)
  --level <error|warning|...>    Default: error
  --timeout-sec <int>            Default: 600 (best-effort; not a hard kill)
  -- <pyright args...>           Extra args passed to Pyright

Examples:
  ./benchmark_scripts/run_pyright_missing_imports.sh --python /opt/scimlopsbench/python
  ./benchmark_scripts/run_pyright_missing_imports.sh --mode system -- --verifytypes yomitoku
EOF
}

mode="system"
repo="."
out_dir="build_output/pyright"
pyright_level="error"
python_bin=""
venv_dir=""
conda_env=""
timeout_sec=600
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --level) pyright_level="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
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

repo="$(cd "$repo" && pwd)"
out_dir="$repo/$out_dir"
mkdir -p "$out_dir"

log_path="$out_dir/log.txt"
pyright_out_json="$out_dir/pyright_output.json"
analysis_json="$out_dir/analysis.json"
results_json="$out_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

echo "[pyright] repo=$repo"
echo "[pyright] out_dir=$out_dir"

py_cmd=()
py_cmd_str=""
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
      py_cmd=(python)
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi
py_cmd_str="${py_cmd[*]}"

cd "$repo"

status="failure"
failure_category="unknown"
skip_reason="unknown"
install_attempted=0
install_command=""
pyright_exit_code=""
decision_reason=""
pyright_cmd_str=""
targets=()
project_args=()

write_minimal_outputs() {
  if [[ ! -f "$pyright_out_json" ]]; then
    echo "{}" >"$pyright_out_json"
  fi
  if [[ ! -f "$analysis_json" ]]; then
    echo "{}" >"$analysis_json"
  fi
  if [[ ! -f "$results_json" ]]; then
    echo "{}" >"$results_json"
  fi
}

write_fallback_results_no_python() {
  local stage_status="${1:-failure}"
  local stage_exit_code=1
  if [[ "$stage_status" == "success" || "$stage_status" == "skipped" ]]; then
    stage_exit_code=0
  fi

  write_minimal_outputs

  cat >"$analysis_json" <<EOF
{
  "missing_packages": [],
  "missing_diagnostics_count": 0,
  "pyright": {},
  "meta": {
    "mode": "$(printf "%s" "$mode" | sed 's/"/\\"/g')",
    "python_cmd": "$(printf "%s" "$py_cmd_str" | sed 's/"/\\"/g')",
    "timeout_sec": $timeout_sec,
    "pyright_level": "$(printf "%s" "$pyright_level" | sed 's/"/\\"/g')",
    "pyright_exit_code": "",
    "install_attempted": $install_attempted,
    "install_command": "$(printf "%s" "$install_command" | sed 's/"/\\"/g')",
    "pyright_command": "",
    "files_scanned": 0,
    "targets": []
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0"
  }
}
EOF

  cat >"$results_json" <<EOF
{
  "status": "$stage_status",
  "skip_reason": "$skip_reason",
  "exit_code": $stage_exit_code,
  "stage": "pyright",
  "task": "check",
  "command": "",
  "timeout_sec": $timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "metrics": {
    "missing_packages_count": 0,
    "total_imported_packages_count": 0,
    "missing_package_ratio": "0/0"
  },
  "meta": {
    "python": "$(printf "%s" "$py_cmd_str" | sed 's/"/\\"/g')",
    "git_commit": null,
    "env_vars": {},
    "decision_reason": "$(printf "%s" "$decision_reason" | sed 's/"/\\"/g')",
    "pyright": {
      "exit_code": "",
      "install_attempted": $install_attempted,
      "install_command": "$(printf "%s" "$install_command" | sed 's/"/\\"/g')"
    }
  },
  "failure_category": "$failure_category",
  "error_excerpt": ""
}
EOF
}

finalize_results() {
  local stage_exit_code="$1"
  local git_commit
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

  local error_excerpt
  error_excerpt="$(tail -n 220 "$log_path" 2>/dev/null || true)"

  OUT_DIR="$out_dir" RESULTS_JSON="$results_json" ANALYSIS_JSON="$analysis_json" PYRIGHT_JSON="$pyright_out_json" \
  MODE="$mode" PY_CMD="$py_cmd_str" TIMEOUT_SEC="$timeout_sec" PYRIGHT_LEVEL="$pyright_level" \
  INSTALL_ATTEMPTED="$install_attempted" INSTALL_COMMAND="$install_command" PYRIGHT_EXIT_CODE="$pyright_exit_code" \
  PYRIGHT_CMD="$pyright_cmd_str" FAILURE_CATEGORY="$failure_category" STATUS="$status" SKIP_REASON="$skip_reason" \
  GIT_COMMIT="$git_commit" DECISION_REASON="$decision_reason" ERROR_EXCERPT="$error_excerpt" \
  TARGETS="$(printf "%s\n" "${targets[@]:-}")" \
  "${py_cmd[@]}" - <<'PY'
import ast
import json
import os
import pathlib
import re
import sys
from typing import Iterable

out_dir = pathlib.Path(os.environ["OUT_DIR"])
results_json = pathlib.Path(os.environ["RESULTS_JSON"])
analysis_json = pathlib.Path(os.environ["ANALYSIS_JSON"])
pyright_json = pathlib.Path(os.environ["PYRIGHT_JSON"])

def safe_load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

pyright_data = safe_load_json(pyright_json)
diagnostics = pyright_data.get("generalDiagnostics", []) if isinstance(pyright_data, dict) else []
missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

pattern = re.compile(r'Import "([^."]+)')
missing_packages = sorted(
    {m.group(1) for d in missing_diags if (m := pattern.search(str(d.get("message", ""))))}
)

def iter_py_files(roots: list[pathlib.Path]) -> Iterable[pathlib.Path]:
    exclude_dirs = {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "build",
        "dist",
        "node_modules",
        "build_output",
        "benchmark_assets",
    }
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in exclude_dirs for part in path.parts):
                continue
            if path in seen:
                continue
            seen.add(path)
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

targets_env = os.environ.get("TARGETS", "")
targets = [t for t in targets_env.splitlines() if t.strip()]
repo_root = pathlib.Path(".").resolve()
roots = [repo_root / t for t in targets] if targets else [repo_root]

all_imported_packages: set[str] = set()
files_scanned = 0
for py_file in iter_py_files(roots):
    files_scanned += 1
    all_imported_packages |= collect_imported_packages(py_file)

missing_packages_count = len(missing_packages)
total_imported_packages_count = len(all_imported_packages)
missing_package_ratio = (
    f"{missing_packages_count}/{total_imported_packages_count}"
    if total_imported_packages_count
    else "0/0"
)

analysis_payload = {
    "missing_packages": missing_packages,
    "missing_diagnostics_count": len(missing_diags),
    "pyright": pyright_data,
    "meta": {
        "mode": os.environ.get("MODE", ""),
        "python_cmd": os.environ.get("PY_CMD", ""),
        "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "0") or 0),
        "pyright_level": os.environ.get("PYRIGHT_LEVEL", ""),
        "pyright_exit_code": os.environ.get("PYRIGHT_EXIT_CODE", ""),
        "install_attempted": os.environ.get("INSTALL_ATTEMPTED", "") == "1",
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "pyright_command": os.environ.get("PYRIGHT_CMD", ""),
        "files_scanned": files_scanned,
        "targets": targets,
    },
    "metrics": {
        "missing_packages_count": missing_packages_count,
        "total_imported_packages_count": total_imported_packages_count,
        "missing_package_ratio": missing_package_ratio,
    },
}

analysis_json.write_text(json.dumps(analysis_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

status = os.environ.get("STATUS", "failure")
failure_category = os.environ.get("FAILURE_CATEGORY", "unknown")
skip_reason = os.environ.get("SKIP_REASON", "unknown")
stage_exit_code = 0 if status in ("success", "skipped") else 1

results_payload = {
    "status": status,
    "skip_reason": skip_reason,
    "exit_code": stage_exit_code,
    "stage": "pyright",
    "task": "check",
    "command": analysis_payload["meta"]["pyright_command"],
    "timeout_sec": analysis_payload["meta"]["timeout_sec"],
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "metrics": analysis_payload["metrics"],
    "meta": {
        "python": analysis_payload["meta"]["python_cmd"],
        "git_commit": os.environ.get("GIT_COMMIT") or None,
        "env_vars": {k: v for k, v in os.environ.items() if k in ("SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "pyright": {
            "exit_code": analysis_payload["meta"]["pyright_exit_code"],
            "install_attempted": analysis_payload["meta"]["install_attempted"],
            "install_command": analysis_payload["meta"]["install_command"],
        },
    },
    "failure_category": failure_category,
    "error_excerpt": os.environ.get("ERROR_EXCERPT", "")[-20000:],
}

results_json.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

  write_minimal_outputs
  exit "$stage_exit_code"
}

# ---- Pyright target selection ----
if [[ -f "pyrightconfig.json" ]]; then
  project_args=(--project pyrightconfig.json)
  decision_reason="Selected pyright targets via pyrightconfig.json (--project pyrightconfig.json)."
elif [[ -f "pyproject.toml" ]] && grep -qE '^[[:space:]]*\\[tool\\.pyright\\][[:space:]]*$' pyproject.toml; then
  project_args=(--project pyproject.toml)
  decision_reason="Selected pyright targets via pyproject.toml ([tool.pyright])."
elif [[ -d "src" ]] && find "src" -type f -name '*.py' -print -quit | grep -q .; then
  targets=(src)
  if [[ -d "tests" ]] && find "tests" -type f -name '*.py' -print -quit | grep -q .; then
    targets+=(tests)
  fi
  decision_reason="Selected pyright targets via src/ layout (${targets[*]})."
else
  mapfile -t targets < <(
    find . -type f -name '__init__.py' \
      -not -path './.git/*' \
      -not -path './.venv/*' \
      -not -path './venv/*' \
      -not -path './build/*' \
      -not -path './dist/*' \
      -not -path './node_modules/*' \
      -not -path './build_output/*' \
      -not -path './benchmark_assets/*' \
      -print \
    | sed 's|^\\./||' \
    | xargs -n1 dirname 2>/dev/null \
    | awk 'NF' \
    | sort -u
  )

  if [[ "${#targets[@]}" -eq 0 ]]; then
    failure_category="entrypoint_not_found"
    status="failure"
    decision_reason="Could not determine pyright targets (no pyrightconfig.json, no [tool.pyright], no src/, no __init__.py packages)."
    echo "[pyright] ERROR: $decision_reason" >&2
    finalize_results 1
  fi
  decision_reason="Selected pyright targets via detected package dirs (__init__.py): ${targets[*]}"
fi

echo "[pyright] decision: $decision_reason"

if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  failure_category="deps"
  status="failure"
  echo "[pyright] ERROR: Failed to run python via: ${py_cmd[*]}" >&2
  write_fallback_results_no_python "failure"
  exit 1
fi

# ---- Ensure pyright is importable (install if missing) ----
if ! "${py_cmd[@]}" -c 'import pyright' >/dev/null 2>&1; then
  install_attempted=1
  install_command="${py_cmd_str} -m pip install -q pyright"
  echo "[pyright] pyright missing; attempting install: $install_command"
  set +e
  "${py_cmd[@]}" -m pip install -q pyright
  pip_rc=$?
  set -e
  if [[ "$pip_rc" -ne 0 ]]; then
    pip_tail="$(tail -n 80 "$log_path" 2>/dev/null || true)"
    if echo "$pip_tail" | grep -qiE 'Temporary failure in name resolution|Name or service not known|Network is unreachable|Connection (timed out|refused|reset)|ProxyError|ReadTimeout|SSLError|TLS|Could not fetch|Failed to establish a new connection'; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    status="failure"
    echo "[pyright] ERROR: Failed to install pyright (rc=$pip_rc)" >&2
    finalize_results 1
  fi
fi

# ---- Run pyright (do not treat non-zero as stage failure) ----
export PYTHONWARNINGS="ignore::SyntaxWarning${PYTHONWARNINGS:+,$PYTHONWARNINGS}"
set +e
if [[ "${#project_args[@]}" -gt 0 ]]; then
  pyright_cmd_str="${py_cmd_str} -m pyright ${project_args[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
  "${py_cmd[@]}" -m pyright "${project_args[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$pyright_out_json"
  pyright_exit_code=$?
else
  pyright_cmd_str="${py_cmd_str} -m pyright ${targets[*]} --level ${pyright_level} --outputjson ${pyright_extra_args[*]}"
  "${py_cmd[@]}" -m pyright "${targets[@]}" --level "$pyright_level" --outputjson "${pyright_extra_args[@]}" >"$pyright_out_json"
  pyright_exit_code=$?
fi
set -e

echo "[pyright] pyright exit code: $pyright_exit_code (ignored for stage status)"

# Validate that pyright_output.json is JSON.
if ! "${py_cmd[@]}" -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$pyright_out_json" >/dev/null 2>&1; then
  failure_category="invalid_json"
  status="failure"
  echo "[pyright] ERROR: pyright_output.json is not valid JSON" >&2
  finalize_results 1
fi

status="success"
failure_category="unknown"
skip_reason="unknown"
finalize_results 0
