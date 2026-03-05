#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


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


def read_stage_results(root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    p = root / "build_output" / stage / "results.json"
    if not p.exists():
        return None, "missing_stage_results"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_out: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES_ORDER:
        data, err = read_stage_results(root, stage)
        stage_results_path = str(root / "build_output" / stage / "results.json")
        stage_log_path = str(root / "build_output" / stage / "log.txt")
        if err:
            stages_out[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": stage_results_path,
                "log_path": stage_log_path,
            }
            failed.append(stage)
            continue

        assert data is not None
        status = str(data.get("status", "failure"))
        exit_code = int(data.get("exit_code", 1)) if str(data.get("exit_code", "")).isdigit() else (0 if status == "success" else 1)
        failure_category = str(data.get("failure_category", "unknown"))
        command = str(data.get("command", ""))
        stages_out[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": stage_results_path,
            "log_path": stage_log_path,
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        # Aggregate metrics when available.
        if stage == "pyright":
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in data:
                    metrics["pyright"][k] = data.get(k)
        if stage == "env_size":
            obs = data.get("observed", {}) if isinstance(data.get("observed"), dict) else {}
            if "env_prefix_size_MB" in obs:
                metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in obs:
                metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
        if stage == "hallucination":
            h = data.get("hallucinations", {}) if isinstance(data.get("hallucinations"), dict) else {}
            for k in ("path", "version", "capability"):
                if isinstance(h.get(k), dict) and "count" in h.get(k):
                    metrics["hallucination"][k] = {"count": h[k]["count"]}

    overall_status = "failure" if failed else "success"

    summary_exit_code = 0 if overall_status == "success" else 1
    summary = {
        "status": "success" if summary_exit_code == 0 else "failure",
        "skip_reason": "not_applicable",
        "exit_code": summary_exit_code,
        "stage": "summary",
        "task": "validate",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary_exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
