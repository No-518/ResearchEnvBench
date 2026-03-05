#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_commit(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Tuple[Dict[str, Any], str]:
    if not path.exists():
        return {}, "missing_stage_results"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception:
        return {}, "invalid_json"


def _stage_summary(stage: str, repo_root: Path) -> Dict[str, Any]:
    results_path = repo_root / f"build_output/{stage}/results.json"
    log_path = repo_root / f"build_output/{stage}/log.txt"
    data, err = _read_json(results_path)
    if err:
        return {
            "status": "failure",
            "exit_code": 1,
            "failure_category": err,
            "command": "",
            "results_path": str(results_path),
            "log_path": str(log_path),
        }
    raw_exit = data.get("exit_code", 1) if isinstance(data, dict) else 1
    try:
        exit_code = int(raw_exit)
    except Exception:
        exit_code = 1
    return {
        "status": str(data.get("status", "failure")),
        "exit_code": exit_code,
        "failure_category": str(data.get("failure_category", "unknown")),
        "command": str(data.get("command", "")),
        "results_path": str(results_path),
        "log_path": str(log_path),
        "raw": data,
    }


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output/summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    for stage in STAGES_IN_ORDER:
        info = _stage_summary(stage, repo_root)
        stages[stage] = {k: v for k, v in info.items() if k != "raw"}
        status = info["status"]
        exit_code = info["exit_code"]
        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

    overall_status = "failure" if failed else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}
    # pyright metrics
    py_raw = (repo_root / "build_output/pyright/results.json")
    py_data, _ = _read_json(py_raw)
    if isinstance(py_data, dict):
        m = py_data.get("metrics", {})
        if isinstance(m, dict):
            metrics["pyright"] = {
                "missing_packages_count": m.get("missing_packages_count", None),
                "total_imported_packages_count": m.get("total_imported_packages_count", None),
                "missing_package_ratio": m.get("missing_package_ratio", None),
            }
    # env size metrics
    env_raw = (repo_root / "build_output/env_size/results.json")
    env_data, _ = _read_json(env_raw)
    if isinstance(env_data, dict):
        obs = env_data.get("observed", {})
        if isinstance(obs, dict):
            metrics["env_size"] = {
                "env_prefix_size_MB": obs.get("env_prefix_size_MB", None),
                "site_packages_total_bytes": obs.get("site_packages_total_bytes", None),
            }
    # hallucination metrics
    hall_raw = (repo_root / "build_output/hallucination/results.json")
    hall_data, _ = _read_json(hall_raw)
    if isinstance(hall_data, dict):
        hs = hall_data.get("hallucinations", {})
        if isinstance(hs, dict):
            metrics["hallucination"] = {
                "path": (hs.get("path", {}) or {}).get("count", None),
                "version": (hs.get("version", {}) or {}).get("count", None),
                "capability": (hs.get("capability", {}) or {}).get("count", None),
            }

    summary = {
        "stage": "summary",
        "status": status,
        "exit_code": exit_code,
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
        },
    }

    log_lines = [
        f"[summary] timestamp_utc={summary['meta']['timestamp_utc']}",
        f"[summary] overall_status={overall_status}",
        f"[summary] failed_stages={failed}",
        f"[summary] skipped_stages={skipped}",
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
