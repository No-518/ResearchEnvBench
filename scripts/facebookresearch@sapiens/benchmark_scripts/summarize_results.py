#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


STAGES_IN_ORDER = [
    "pyright",
    "prepare",
    "cpu",
    "cuda",
    "single_gpu",
    "multi_gpu",
    "env_size",
    "hallucination",
]


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_last_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _git_commit(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_summary: dict[str, Any] = {}
    failed_stages: list[str] = []
    skipped_stages: list[str] = []

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("w", encoding="utf-8") as log_f:
        for stage in STAGES_IN_ORDER:
            stage_dir = repo_root / "build_output" / stage
            res_path = stage_dir / "results.json"
            stage_entry: dict[str, Any] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_stage_results",
                "command": "",
                "results_path": str(res_path),
                "log_path": str(stage_dir / "log.txt"),
            }

            if not res_path.exists():
                log_f.write(f"[summary] missing results: {res_path}\n")
            else:
                try:
                    data = _read_json(res_path)
                    stage_entry["status"] = data.get("status", "failure")
                    stage_entry["exit_code"] = int(data.get("exit_code", 1))
                    stage_entry["failure_category"] = data.get("failure_category", "") or ""
                    stage_entry["command"] = data.get("command", "") or ""
                    stage_entry["raw"] = {
                        "stage": data.get("stage", stage),
                        "task": data.get("task", ""),
                    }
                except json.JSONDecodeError:
                    log_f.write(f"[summary] invalid json: {res_path}\n")
                    stage_entry["status"] = "failure"
                    stage_entry["exit_code"] = 1
                    stage_entry["failure_category"] = "invalid_json"
                except Exception as e:
                    log_f.write(f"[summary] error reading {res_path}: {e!r}\n")
                    stage_entry["status"] = "failure"
                    stage_entry["exit_code"] = 1
                    stage_entry["failure_category"] = "invalid_json"

            stages_summary[stage] = stage_entry

            if stage_entry["status"] == "skipped":
                skipped_stages.append(stage)
            elif stage_entry["status"] == "failure" or int(stage_entry["exit_code"]) == 1:
                failed_stages.append(stage)

        # Aggregate metrics where present.
        pyright_res = (repo_root / "build_output" / "pyright" / "results.json")
        if pyright_res.exists():
            try:
                d = _read_json(pyright_res)
                metrics["pyright"] = {
                    "missing_packages_count": d.get("missing_packages_count"),
                    "total_imported_packages_count": d.get("total_imported_packages_count"),
                    "missing_package_ratio": d.get("missing_package_ratio"),
                }
            except Exception:
                pass

        env_res = (repo_root / "build_output" / "env_size" / "results.json")
        if env_res.exists():
            try:
                d = _read_json(env_res)
                obs = d.get("observed") or {}
                metrics["env_size"] = {
                    "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                    "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                }
            except Exception:
                pass

        hallu_res = (repo_root / "build_output" / "hallucination" / "results.json")
        if hallu_res.exists():
            try:
                d = _read_json(hallu_res)
                h = d.get("hallucinations") or {}
                metrics["hallucination"] = {
                    "path_hallucinations": (h.get("path") or {}).get("count"),
                    "version_hallucinations": (h.get("version") or {}).get("count"),
                    "capability_hallucinations": (h.get("capability") or {}).get("count"),
                }
            except Exception:
                pass

    overall_status = "failure" if failed_stages else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0
    summary: dict[str, Any] = {
        "stage": "summary",
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "summarize",
        "command": f"{sys.executable} {Path(__file__).resolve()}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": _git_commit(repo_root),
            "env_vars": {},
            "decision_reason": "Aggregate per-stage results.json into a single ordered summary and compute overall pass/fail.",
            "timestamp_utc": _now_utc_iso(),
        },
        "failure_category": "" if overall_status == "success" else "unknown",
        "error_excerpt": _read_last_lines(log_path, max_lines=240),
    }
    _write_json(results_path, summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
