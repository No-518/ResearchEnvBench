#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str) -> None:
    _safe_mkdir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    if not path.exists():
        return None, "missing_stage_results"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception:
        return None, "invalid_json"


def _git_commit() -> str:
    try:
        import subprocess

        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), text=True).strip()
        return out
    except Exception:
        return ""


def main() -> int:
    out_dir = REPO_ROOT / "build_output" / "summary"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    _safe_mkdir(out_dir)
    _write_text(
        log_path,
        f"stage=summary\nrepo={REPO_ROOT}\nout_dir={out_dir}\ntimestamp_utc={_utc_timestamp()}\n",
    )

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    for stage in STAGES_ORDER:
        stage_dir = REPO_ROOT / "build_output" / stage
        results_file = stage_dir / "results.json"
        log_file = stage_dir / "log.txt"

        data, err = _load_json(results_file)
        if data is None:
            # Missing or invalid results -> mark failure for summary only.
            status = "failure"
            exit_code = 1
            failure_category = err or "unknown"
            command = ""
        else:
            status = str(data.get("status", "failure"))
            raw_exit = data.get("exit_code", 1)
            try:
                exit_code = int(raw_exit)
            except Exception:
                exit_code = 1
            failure_category = str(data.get("failure_category", "unknown"))
            command = str(data.get("command", ""))

        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(results_file),
            "log_path": str(log_file),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

    overall_status = "failure" if failed else "success"

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    # Aggregate pyright metrics.
    pyright_data, _ = _load_json(REPO_ROOT / "build_output" / "pyright" / "results.json")
    if pyright_data:
        for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
            if k in pyright_data:
                metrics["pyright"][k] = pyright_data.get(k)

    # Aggregate env_size metrics.
    env_data, _ = _load_json(REPO_ROOT / "build_output" / "env_size" / "results.json")
    if env_data and isinstance(env_data.get("observed"), dict):
        obs = env_data["observed"]
        if "env_prefix_size_MB" in obs:
            metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
        if "site_packages_total_bytes" in obs:
            metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")

    # Aggregate hallucination counts.
    hall_data, _ = _load_json(REPO_ROOT / "build_output" / "hallucination" / "results.json")
    if hall_data and isinstance(hall_data.get("hallucinations"), dict):
        h = hall_data["hallucinations"]
        for key in ["path", "version", "capability"]:
            if isinstance(h.get(key), dict) and "count" in h[key]:
                metrics["hallucination"][f"{key}_count"] = h[key].get("count")

    summary = {
        "status": "success" if overall_status == "success" else "failure",
        "skip_reason": "unknown",
        "exit_code": 0 if overall_status == "success" else 1,
        "stage": "summary",
        "task": "summarize",
        "command": f"{sys.executable} benchmark_scripts/summarize_results.py",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {
            "git_commit": _git_commit(),
            "timestamp_utc": _utc_timestamp(),
            "report_path": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    _write_json(results_path, summary)

    with log_path.open("a", encoding="utf-8") as f:
        f.write("\nfailed_stages=" + ",".join(failed) + "\n")
        f.write("skipped_stages=" + ",".join(skipped) + "\n")
        f.write("overall_status=" + overall_status + "\n")

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
