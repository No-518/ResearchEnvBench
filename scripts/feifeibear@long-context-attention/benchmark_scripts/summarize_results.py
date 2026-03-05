#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES_ORDER = [
    "pyright",
    "prepare",
    "cpu",
    "cuda",
    "single_gpu",
    "multi_gpu",
    "env_size",
    "hallucination",
]


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .strip()
        )
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, "missing_stage_results"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception:
        return None, "invalid_json"


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    cmd_str = " ".join(shlex.quote(a) for a in sys.argv)

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES_ORDER:
        stage_dir = root / "build_output" / stage
        res_path = stage_dir / "results.json"
        log_p = stage_dir / "log.txt"
        data, err = read_json(res_path)
        if err:
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(res_path),
                "log_path": str(log_p),
            }
            failed.append(stage)
            continue

        status = str(data.get("status") or "failure")
        raw_exit = data.get("exit_code", 1)
        try:
            exit_code = int(raw_exit)
        except Exception:
            exit_code = 1
        failure_category = str(data.get("failure_category") or "unknown")
        command = str(data.get("command") or "")
        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(res_path),
            "log_path": str(log_p),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        # Aggregate metrics
        if stage == "pyright":
            m = data.get("metrics") or {}
            if isinstance(m, dict):
                metrics["pyright"] = {
                    "missing_packages_count": m.get("missing_packages_count"),
                    "total_imported_packages_count": m.get("total_imported_packages_count"),
                    "missing_package_ratio": m.get("missing_package_ratio"),
                }
        elif stage == "env_size":
            obs = data.get("observed") or {}
            if isinstance(obs, dict):
                metrics["env_size"] = {
                    "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                    "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                }
        elif stage == "hallucination":
            h = data.get("hallucinations") or {}
            if isinstance(h, dict):
                metrics["hallucination"] = {
                    "path": (h.get("path") or {}).get("count") if isinstance(h.get("path"), dict) else None,
                    "version": (h.get("version") or {}).get("count") if isinstance(h.get("version"), dict) else None,
                    "capability": (h.get("capability") or {}).get("count") if isinstance(h.get("capability"), dict) else None,
                }

    overall_status = "failure" if failed else "success"
    status = "success" if overall_status == "success" else "failure"
    exit_code = 0 if overall_status == "success" else 1
    failure_category = "unknown" if exit_code == 0 else "runtime"
    error_excerpt = "" if exit_code == 0 else f"failed_stages={failed}"

    summary = {
        "stage": "summary",
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "task": "validate",
        "command": cmd_str,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {
                "path": str((root / "benchmark_assets" / "dataset").resolve()),
                "source": "not_applicable",
                "version": "unknown",
                "sha256": "",
            },
            "model": {
                "path": str((root / "benchmark_assets" / "model").resolve()),
                "source": "not_applicable",
                "version": "unknown",
                "sha256": "",
            },
        },
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "decision_reason": "Aggregate per-stage results.json into a single summary and set overall_status=failure if any stage failed (skipped excluded).",
            "timestamp_utc": utc(),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[summary] timestamp_utc={summary['meta']['timestamp_utc']}\n")
        log.write(f"[summary] overall_status={overall_status}\n")
        log.write(f"[summary] failed_stages={failed}\n")
        log.write(f"[summary] skipped_stages={skipped}\n")

    write_json(results_path, summary)
    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
