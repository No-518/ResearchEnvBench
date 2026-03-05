#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit(repo_root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _read_json(path: Path) -> Tuple[Any, str]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), "ok"
    except Exception as e:
        return None, f"invalid_json:{e}"

def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def main() -> int:
    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "summary"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    summary_stages: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    summary_failure_category = "unknown"
    summary_error_excerpt = ""

    for stage in STAGES_IN_ORDER:
        p = repo_root / "build_output" / stage / "results.json"
        data, state = _read_json(p)
        if state != "ok" or not isinstance(data, dict):
            failure_category = "missing_stage_results" if state == "missing" else "invalid_json"
            summary_failure_category = failure_category if summary_failure_category == "unknown" else summary_failure_category
            stage_obj = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": failure_category,
                "command": "",
                "results_path": str(p),
                "log_path": str(repo_root / "build_output" / stage / "log.txt"),
            }
            summary_stages[stage] = stage_obj
            failed_stages.append(stage)
            continue

        status = str(data.get("status", "failure"))
        exit_code = _coerce_int(data.get("exit_code", 1), 1)
        failure_category = data.get("failure_category", "unknown")
        command = data.get("command", "")

        summary_stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(p),
            "log_path": str(repo_root / "build_output" / stage / "log.txt"),
        }

        if status == "failure" or exit_code == 1:
            if summary_failure_category == "unknown" and failure_category:
                summary_failure_category = str(failure_category)
            failed_stages.append(stage)
        elif status == "skipped":
            skipped_stages.append(stage)

        # Pull metrics when available.
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
            hall = data.get("hallucinations") or {}
            if isinstance(hall, dict):
                metrics["hallucination"] = {
                    "path_hallucinations": (hall.get("path") or {}).get("count"),
                    "version_hallucinations": (hall.get("version") or {}).get("count"),
                    "capability_hallucinations": (hall.get("capability") or {}).get("count"),
                }

    overall_status = "failure" if failed_stages else "success"
    exit_code = 1 if overall_status == "failure" else 0

    payload = {
        "status": overall_status,
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": summary_stages,
        "metrics": metrics,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp()},
        "failure_category": "unknown" if overall_status == "success" else summary_failure_category,
        "error_excerpt": summary_error_excerpt,
    }
    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"overall_status={overall_status} failed_stages={failed_stages} skipped_stages={skipped_stages}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
