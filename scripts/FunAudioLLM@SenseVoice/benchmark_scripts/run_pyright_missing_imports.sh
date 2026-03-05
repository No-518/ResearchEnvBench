#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run Pyright in an already-configured environment and report only missing-import diagnostics.

Outputs (always written, even on failure):
  build_output/pyright/log.txt
  build_output/pyright/pyright_output.json
  build_output/pyright/analysis.json
  build_output/pyright/results.json

Required:
  --repo <path>                  Path to the repository/project to analyze (must be this repo root)

Environment selection (pick ONE):
  --python <path>                Explicit python executable to use (highest priority)
  --mode venv   --venv <path>    Use <venv>/bin/python
  --mode uv    [--venv <path>]   Use <venv>/bin/python (default: .venv)
  --mode conda  --conda-env <n>  Resolve python via: conda run -n <n> python
  --mode poetry                  Resolve python via: poetry run python
  --mode system                  Use python from PATH

Optional:
  --out-dir <path>               Default: build_output/pyright
  --level <error|warning|...>    Default: error
  --timeout-sec <int>            Default: 600
  --report-path <path>           Used by runner.py if --python not provided (default: /opt/scimlopsbench/report.json)
  -- <pyright args...>           Extra args passed to Pyright (e.g. --pythonversion 3.10)
EOF
}

repo=""
out_dir="build_output/pyright"
pyright_level="error"
timeout_sec=600
python_bin=""
mode="system"
mode_specified=0
venv_dir=""
conda_env=""
report_path=""
pyright_extra_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      mode="${2:-}"; mode_specified=1; shift 2 ;;
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(cd "$repo" && pwd)" != "$REPO_ROOT" ]]; then
  echo "--repo must be this repo root: $REPO_ROOT" >&2
  exit 2
fi

mkdir -p "$out_dir"

ASSETS_ROOT="benchmark_assets"
CACHE_ROOT="$ASSETS_ROOT/cache"
HOME_DIR="$CACHE_ROOT/home"
XDG_CACHE_HOME="$CACHE_ROOT/xdg_cache"
PIP_CACHE_DIR="$CACHE_ROOT/pip"
mkdir -p "$CACHE_ROOT" "$HOME_DIR" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR"

extra_args_json="$out_dir/pyright_extra_args.json"
runner_py="$(command -v python3 || command -v python)"
"$runner_py" - "$extra_args_json" "${pyright_extra_args[@]+"${pyright_extra_args[@]}"}" <<'PY'
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
out.write_text(json.dumps(sys.argv[2:]), encoding="utf-8")
PY

resolved_python=""
if [[ -n "$python_bin" ]]; then
  resolved_python="$python_bin"
elif [[ "$mode_specified" -eq 1 ]]; then
  case "$mode" in
    venv)
      [[ -n "$venv_dir" ]] || { echo "--venv is required for --mode venv" >&2; exit 2; }
      resolved_python="$venv_dir/bin/python"
      ;;
    uv)
      venv_dir="${venv_dir:-.venv}"
      resolved_python="$venv_dir/bin/python"
      ;;
    conda)
      [[ -n "$conda_env" ]] || { echo "--conda-env is required for --mode conda" >&2; exit 2; }
      command -v conda >/dev/null 2>&1 || { echo "conda not found in PATH" >&2; exit 2; }
      resolved_python="$(conda run -n "$conda_env" python -c 'import sys; print(sys.executable)')"
      ;;
    poetry)
      command -v poetry >/dev/null 2>&1 || { echo "poetry not found in PATH" >&2; exit 2; }
      resolved_python="$(poetry run python -c 'import sys; print(sys.executable)')"
      ;;
    system)
      resolved_python="$(python -c 'import sys; print(sys.executable)')"
      ;;
    *)
      echo "Unknown --mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
fi

cmd="$(cat <<'BASH'
set -euo pipefail

OUT_DIR="${PYRIGHT_OUT_DIR}"
mkdir -p "$OUT_DIR"

"$BENCH_PYTHON" - <<'PY'
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import traceback
from typing import Iterable

repo = pathlib.Path(os.environ["PYRIGHT_REPO"]).resolve()
out_dir = pathlib.Path(os.environ["PYRIGHT_OUT_DIR"]).resolve()
level = os.environ.get("PYRIGHT_LEVEL", "error")
extra_args_path = pathlib.Path(os.environ["PYRIGHT_EXTRA_ARGS_JSON"])

out_json = out_dir / "pyright_output.json"
analysis_json = out_dir / "analysis.json"
stage_extra_json = out_dir / "extra_results.json"

install_attempted = False
install_cmd = [sys.executable, "-m", "pip", "install", "-q", "pyright"]
install_ok = None
install_error = ""

def write_json(path: pathlib.Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def detect_targets(repo_root: pathlib.Path):
    pyrightconfig = repo_root / "pyrightconfig.json"
    if pyrightconfig.is_file():
        return ["--project", str(pyrightconfig)]

    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            txt = pyproject.read_text(encoding="utf-8", errors="replace")
        except Exception:
            txt = ""
        if "[tool.pyright]" in txt:
            return ["--project", str(pyproject)]

    src = repo_root / "src"
    if src.is_dir():
        targets = [str(src)]
        tests = repo_root / "tests"
        if tests.is_dir():
            targets.append(str(tests))
        return targets

    exclude_parts = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "build",
        "dist",
        "benchmark_assets",
        "benchmark_scripts",
        "build_output",
    }
    pkg_dirs = set()
    for init_file in repo_root.rglob("__init__.py"):
        if any(p in exclude_parts for p in init_file.parts):
            continue
        pkg_dirs.add(str(init_file.parent))
    return sorted(pkg_dirs)

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
        "benchmark_scripts",
        "build_output",
    }
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path

def collect_imported_packages(py_file: pathlib.Path) -> set:
    pkgs: set = set()
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

try:
    try:
        import pyright  # noqa: F401
        pyright_present = True
    except Exception:
        pyright_present = False

    if not pyright_present:
        install_attempted = True
        try:
            r = subprocess.run(install_cmd, cwd=str(repo), capture_output=True, text=True, check=False)
            install_ok = (r.returncode == 0)
            if r.stdout:
                print(r.stdout)
            if r.stderr:
                print(r.stderr, file=sys.stderr)
            if not install_ok:
                install_error = (r.stdout or "") + "\n" + (r.stderr or "")
        except Exception as e:
            install_ok = False
            install_error = str(e)

        if not install_ok:
            write_json(out_json, {"error": "pyright_install_failed"})
            write_json(analysis_json, {"error": "pyright_install_failed"})
            write_json(
                stage_extra_json,
                {
                    "missing_packages_count": 0,
                    "total_imported_packages_count": 0,
                    "missing_package_ratio": "0/0",
                    "failure_category": "deps",
                    "meta": {
                        "pyright_install_attempted": True,
                        "pyright_install_command": " ".join(install_cmd),
                        "pyright_install_ok": False,
                    },
                },
            )
            sys.exit(1)

    targets = detect_targets(repo)
    if not targets:
        write_json(out_json, {"error": "no_targets"})
        write_json(analysis_json, {"error": "no_targets"})
        write_json(
            stage_extra_json,
            {
                "missing_packages_count": 0,
                "total_imported_packages_count": 0,
                "missing_package_ratio": "0/0",
                "failure_category": "entrypoint_not_found",
                "meta": {
                    "pyright_install_attempted": install_attempted,
                    "pyright_install_command": " ".join(install_cmd),
                    "pyright_install_ok": True if install_attempted else None,
                },
            },
        )
        sys.exit(1)

    try:
        extra_args = json.loads(extra_args_path.read_text(encoding="utf-8"))
        if not isinstance(extra_args, list):
            extra_args = []
    except Exception:
        extra_args = []

    pyright_cmd = [sys.executable, "-m", "pyright", *targets, "--level", level, "--outputjson", *extra_args]
    r = subprocess.run(pyright_cmd, cwd=str(repo), capture_output=True, text=True, check=False)

    # Pyright may exit non-zero even when it produces valid JSON.
    raw = r.stdout.strip()
    if not raw:
        raw = json.dumps({"generalDiagnostics": [], "error": r.stderr[-4000:] if r.stderr else ""})
    out_json.write_text(raw + "\n", encoding="utf-8")

    data = json.loads(raw)
    diagnostics = data.get("generalDiagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []

    missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]
    pattern = re.compile(r'Import \"([^.\\\"]+)')
    missing_packages = sorted(
        {
            m.group(1)
            for d in missing_diags
            if isinstance(d.get("message"), str) and (m := pattern.search(d.get("message", "")))
        }
    )

    all_imported_packages = set()
    files_scanned = 0
    for py_file in iter_py_files(repo):
        files_scanned += 1
        all_imported_packages |= collect_imported_packages(py_file)

    missing_packages_count = len(missing_packages)
    total_imported_packages_count = len(all_imported_packages)
    missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

    analysis_payload = {
        "missing_packages": missing_packages,
        "pyright": data,
        "meta": {
            "targets": targets,
            "pyright_cmd": pyright_cmd,
            "files_scanned": files_scanned,
            "pyright_install_attempted": install_attempted,
            "pyright_install_command": " ".join(install_cmd),
            "pyright_install_ok": install_ok,
        },
        "metrics": {
            "missing_packages_count": missing_packages_count,
            "total_imported_packages_count": total_imported_packages_count,
            "missing_package_ratio": missing_package_ratio,
        },
    }
    write_json(analysis_json, analysis_payload)
    write_json(
        stage_extra_json,
        {
            **analysis_payload["metrics"],
            "meta": {
                "pyright_install_attempted": install_attempted,
                "pyright_install_command": " ".join(install_cmd),
                "pyright_install_ok": install_ok,
            },
        },
    )
except Exception:
    traceback.print_exc()
    try:
        write_json(out_json, {"error": "pyright_stage_exception"})
    except Exception:
        pass
    try:
        write_json(analysis_json, {"error": "pyright_stage_exception"})
    except Exception:
        pass
    try:
        write_json(stage_extra_json, {"failure_category": "unknown"})
    except Exception:
        pass
    sys.exit(1)
PY
BASH
)"

exec "$runner_py" benchmark_scripts/runner.py \
  --stage pyright \
  --task check \
  --framework unknown \
  --timeout-sec "$timeout_sec" \
  --out-dir "$out_dir" \
  --requires-python \
  ${report_path:+--report-path "$report_path"} \
  ${resolved_python:+--python "$resolved_python"} \
  --env "HOME=$HOME_DIR" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  --env "PIP_CACHE_DIR=$PIP_CACHE_DIR" \
  --env "PYTHONDONTWRITEBYTECODE=1" \
  --env "PYTHONUNBUFFERED=1" \
  --env "PYRIGHT_REPO=$REPO_ROOT" \
  --env "PYRIGHT_OUT_DIR=$out_dir" \
  --env "PYRIGHT_LEVEL=$pyright_level" \
  --env "PYRIGHT_EXTRA_ARGS_JSON=$extra_args_json" \
  --extra-json "$out_dir/extra_results.json" \
  -- bash -lc "$cmd"
