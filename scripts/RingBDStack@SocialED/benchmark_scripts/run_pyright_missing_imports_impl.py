#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, "missing_report"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def run_capture(argv: List[str], cwd: Path) -> Tuple[int, str]:
    try:
        completed = subprocess.run(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return int(completed.returncode), completed.stdout
    except FileNotFoundError as e:
        return 127, str(e)
    except Exception as e:
        return 1, str(e)


def detect_targets(repo_dir: Path) -> Tuple[List[str], List[str], str]:
    # Priority:
    # 1) pyrightconfig.json -> --project pyrightconfig.json
    # 2) pyproject.toml with [tool.pyright] -> --project pyproject.toml
    # 3) src/ layout -> targets = src (+ tests)
    # 4) package dirs (__init__.py) -> targets = detected dirs
    # 5) none -> fail
    if (repo_dir / "pyrightconfig.json").exists():
        return ["."], ["--project", "pyrightconfig.json"], "Found pyrightconfig.json; using --project pyrightconfig.json."

    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            txt = pyproject.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^\\[tool\\.pyright\\]\\s*$", txt, flags=re.MULTILINE):
                return ["."], ["--project", "pyproject.toml"], "Found [tool.pyright] in pyproject.toml; using --project pyproject.toml."
        except Exception:
            pass

    if (repo_dir / "src").is_dir():
        targets = ["src"]
        if (repo_dir / "tests").is_dir():
            targets.append("tests")
        return targets, [], "Detected src/ layout; targeting src (and tests if present)."

    exclude = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules", "build_output", "benchmark_assets"}
    pkg_dirs: List[Path] = []
    for init in repo_dir.rglob("__init__.py"):
        if any(part in exclude for part in init.parts):
            continue
        pkg_dirs.append(init.parent)

    pkg_dirs = sorted(set(pkg_dirs), key=lambda p: (len(p.parts), str(p)))
    top: List[Path] = []
    for d in pkg_dirs:
        # filter out subpackages
        if any((parent / "__init__.py").exists() for parent in d.parents if parent != repo_dir and parent in pkg_dirs):
            continue
        if d == repo_dir:
            continue
        top.append(d)

    if top:
        return [p.relative_to(repo_dir).as_posix() for p in top], [], "Detected package dirs via __init__.py; targeting top-level packages."

    return [], [], "No pyright targets found."


def iter_py_files(repo_dir: Path, targets: List[str]) -> Iterable[Path]:
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
    roots = [repo_dir / t for t in targets] if targets else [repo_dir]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in exclude_dirs for part in path.parts):
                continue
            yield path


def collect_imported_packages(py_file: Path) -> set[str]:
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


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tail_log(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Pyright and summarize missing-import diagnostics.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out-root", default="build_output")
    ap.add_argument("--level", default="error")
    ap.add_argument("--python", default=None)
    ap.add_argument("--mode", default="system", choices=["venv", "uv", "conda", "poetry", "system"])
    ap.add_argument("--venv", default=None)
    ap.add_argument("--conda-env", default=None)
    ap.add_argument("--install-pyright", action="store_true")
    ap.add_argument("--report-path", default=None, help="Override report.json path when --mode system and no --python.")
    ap.add_argument("pyright_extra_args", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    root = repo_root()
    repo_dir = Path(args.repo).resolve()
    stage_dir = root / args.out_root / "pyright"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    pyright_output = stage_dir / "pyright_output.json"
    analysis_json = stage_dir / "analysis.json"
    results_json = stage_dir / "results.json"

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    # Always start a fresh log.
    log_path.write_text("", encoding="utf-8")
    log(f"[pyright] utc={utc_timestamp()}")
    log(f"[pyright] repo={repo_dir}")

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "pyright",
        "task": "check",
        "command": "",
        "timeout_sec": 600,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "timestamp_utc": utc_timestamp(),
            "python": "",
            "git_commit": git_commit(root),
            "env_vars": {"python_resolution": "", "install_attempted": False, "install_cmd": ""},
            "decision_reason": "",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    # Resolve python command.
    python_cmd: List[str] = []
    python_resolution = ""
    if args.python:
        python_cmd = [args.python]
        python_resolution = "cli"
    else:
        if args.mode == "venv":
            if not args.venv:
                log("[pyright] ERROR: --venv required for --mode venv")
                results["failure_category"] = "args_unknown"
                results["error_excerpt"] = "Missing --venv for --mode venv"
                write_json(pyright_output, {})
                write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
                write_json(results_json, results)
                return 1
            python_cmd = [str(Path(args.venv) / "bin" / "python")]
            python_resolution = "venv"
        elif args.mode == "uv":
            v = args.venv or ".venv"
            python_cmd = [str(Path(v) / "bin" / "python")]
            python_resolution = "uv"
        elif args.mode == "conda":
            if not args.conda_env:
                log("[pyright] ERROR: --conda-env required for --mode conda")
                results["failure_category"] = "args_unknown"
                results["error_excerpt"] = "Missing --conda-env for --mode conda"
                write_json(pyright_output, {})
                write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
                write_json(results_json, results)
                return 1
            python_cmd = ["conda", "run", "-n", args.conda_env, "python"]
            python_resolution = "conda"
        elif args.mode == "poetry":
            python_cmd = ["poetry", "run", "python"]
            python_resolution = "poetry"
        else:
            if os.environ.get("SCIMLOPSBENCH_PYTHON"):
                python_cmd = [os.environ["SCIMLOPSBENCH_PYTHON"]]
                python_resolution = "env:SCIMLOPSBENCH_PYTHON"
            else:
                report_path = resolve_report_path(args.report_path)
                report, err = load_report(report_path)
                if report and isinstance(report.get("python_path"), str) and report.get("python_path"):
                    python_cmd = [str(report["python_path"])]
                    python_resolution = "report.json"
                else:
                    python_cmd = [sys.executable]
                    python_resolution = "sys.executable"

    results["meta"]["env_vars"]["python_resolution"] = python_resolution
    results["meta"]["python"] = " ".join(python_cmd)
    results["command"] = " ".join(python_cmd + ["-m", "pyright", "..."])

    # Verify python command can run.
    rc, out = run_capture(python_cmd + ["-c", "import sys; print(sys.executable)"], cwd=repo_dir)
    log(f"[pyright] python_resolution={python_resolution}")
    log(f"[pyright] python_cmd={' '.join(python_cmd)}")
    if rc != 0:
        log(f"[pyright] ERROR: python exec failed rc={rc}: {out}")
        results["failure_category"] = "deps"
        results["error_excerpt"] = tail_log(log_path)
        write_json(pyright_output, {})
        write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
        write_json(results_json, results)
        return 1

    # Ensure pyright is available inside this interpreter environment.
    install_attempted = False
    install_cmd = ""
    rc, _ = run_capture(python_cmd + ["-c", "import pyright"], cwd=repo_dir)
    if rc != 0:
        if not args.install_pyright:
            log("[pyright] ERROR: pyright not installed; re-run with --install-pyright")
            results["failure_category"] = "deps"
            results["error_excerpt"] = "pyright import failed"
            write_json(pyright_output, {})
            write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
            write_json(results_json, results)
            return 1
        install_attempted = True
        install_cmd = " ".join(python_cmd + ["-m", "pip", "install", "-q", "pyright"])
        log(f"[pyright] Installing pyright via: {install_cmd}")
        irc, iout = run_capture(python_cmd + ["-m", "pip", "install", "-q", "pyright"], cwd=repo_dir)
        log(iout)
        if irc != 0:
            results["failure_category"] = "download_failed" if "http" in iout.lower() or "connection" in iout.lower() else "deps"
            results["error_excerpt"] = "pyright install failed"
            results["meta"]["env_vars"]["install_attempted"] = True
            results["meta"]["env_vars"]["install_cmd"] = install_cmd
            write_json(pyright_output, {})
            write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
            write_json(results_json, results)
            return 1

    results["meta"]["env_vars"]["install_attempted"] = install_attempted
    results["meta"]["env_vars"]["install_cmd"] = install_cmd

    # Detect pyright targets.
    targets, project_args, decision_reason = detect_targets(repo_dir)
    results["meta"]["decision_reason"] = decision_reason
    if not targets:
        log("[pyright] ERROR: No python targets found for pyright.")
        results["failure_category"] = "entrypoint_not_found"
        results["error_excerpt"] = "No python targets found for pyright."
        write_json(pyright_output, {})
        write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
        write_json(results_json, results)
        return 1

    # Run pyright (non-zero exit does not fail this stage; we only fail if output cannot be parsed).
    extra = [a for a in args.pyright_extra_args if a != "--"]
    pyright_cmd = python_cmd + ["-m", "pyright", *targets, "--level", args.level, "--outputjson", *project_args, *extra]
    results["command"] = " ".join(pyright_cmd)
    log(f"[pyright] targets={targets}")
    log(f"[pyright] pyright_cmd={' '.join(pyright_cmd)}")

    try:
        completed = subprocess.run(
            pyright_cmd,
            cwd=str(repo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pyright_output.write_text(completed.stdout or "{}", encoding="utf-8")
        pyright_exit = int(completed.returncode)
        log(f"[pyright] pyright_exit_code={pyright_exit}")
    except Exception as e:
        log(f"[pyright] ERROR: pyright execution failed: {e}")
        results["failure_category"] = "runtime"
        results["error_excerpt"] = tail_log(log_path)
        write_json(pyright_output, {})
        write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
        write_json(results_json, results)
        return 1

    # Parse pyright JSON output.
    try:
        py_data = json.loads(pyright_output.read_text(encoding="utf-8"))
        if not isinstance(py_data, dict):
            raise ValueError("pyright output not a JSON object")
    except Exception as e:
        log(f"[pyright] ERROR: failed to parse pyright_output.json: {e}")
        results["failure_category"] = "invalid_json"
        results["error_excerpt"] = tail_log(log_path)
        write_json(analysis_json, {"missing_packages": [], "pyright": {}, "meta": {}, "metrics": {}})
        write_json(results_json, results)
        return 1

    diagnostics = py_data.get("generalDiagnostics", []) if isinstance(py_data.get("generalDiagnostics"), list) else []
    missing_diags = [d for d in diagnostics if isinstance(d, dict) and d.get("rule") == "reportMissingImports"]

    pattern = re.compile(r'Import "([^."]+)')
    missing_packages = sorted(
        {m.group(1) for d in missing_diags if isinstance(d.get("message", ""), str) and (m := pattern.search(d.get("message", "")))}
    )

    all_imported: set[str] = set()
    files_scanned = 0
    for f in iter_py_files(repo_dir, targets):
        files_scanned += 1
        all_imported |= collect_imported_packages(f)

    missing_packages_count = len(missing_packages)
    total_imported_packages_count = len(all_imported)
    missing_package_ratio = f"{missing_packages_count}/{total_imported_packages_count}"

    analysis_payload = {
        "missing_packages": missing_packages,
        "pyright": py_data,
        "meta": {
            "timestamp_utc": utc_timestamp(),
            "python_cmd": " ".join(python_cmd),
            "python_resolution": python_resolution,
            "install_attempted": install_attempted,
            "install_cmd": install_cmd,
            "pyright_exit_code": pyright_exit,
            "targets": targets,
            "project_args": project_args,
            "files_scanned": files_scanned,
            "decision_reason": decision_reason,
        },
        "metrics": {
            "missing_packages_count": missing_packages_count,
            "total_imported_packages_count": total_imported_packages_count,
            "missing_package_ratio": missing_package_ratio,
        },
    }
    write_json(analysis_json, analysis_payload)

    results.update(
        {
            "status": "success",
            "skip_reason": "not_applicable",
            "exit_code": 0,
            "failure_category": "not_applicable",
            "error_excerpt": "",
            "missing_packages_count": missing_packages_count,
            "total_imported_packages_count": total_imported_packages_count,
            "missing_package_ratio": missing_package_ratio,
            "missing_packages": missing_packages,
            "meta": {
                **results["meta"],
                "analysis_path": str(analysis_json),
                "pyright_output_path": str(pyright_output),
                "pyright_exit_code": pyright_exit,
            },
        }
    )
    write_json(results_json, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
