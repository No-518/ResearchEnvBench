#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGE_ORDER = [
    "pyright",
    "prepare",
    "cpu",
    "cuda",
    "single_gpu",
    "multi_gpu",
    "env_size",
    "hallucination",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(repo: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), text=True).strip()
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"invalid_json: {e}"

def empty_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_assets(repo: Path) -> Dict[str, Any]:
    p = repo / "build_output" / "prepare" / "results.json"
    d, _ = load_json(p)
    if isinstance(d, dict) and isinstance(d.get("assets"), dict):
        return d["assets"]
    return empty_assets()


def main() -> int:
    repo = repo_root()
    out_dir = repo / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_summary: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}
    assets = load_assets(repo)

    with log_path.open("w", encoding="utf-8") as log:
        for stage in STAGE_ORDER:
            results_file = repo / "build_output" / stage / "results.json"
            stage_log = repo / "build_output" / stage / "log.txt"
            data, err = load_json(results_file)
            if data is None:
                failure_category = "missing_stage_results" if err == "missing" else "invalid_json"
                data = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": failure_category,
                    "command": "",
                }
                log.write(f"[summary] {stage}: {failure_category} ({err})\n")

            status = data.get("status", "failure")
            exit_code = data.get("exit_code", 1)
            failure_category = data.get("failure_category", "unknown")
            command = data.get("command", "")

            stages_summary[stage] = {
                "status": status,
                "exit_code": int(exit_code) if isinstance(exit_code, int) else 1,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(results_file),
                "log_path": str(stage_log),
            }

            if status == "skipped":
                skipped.append(stage)
            elif status == "failure" or int(exit_code) == 1:
                failed.append(stage)

            # Aggregated metrics.
            if stage == "pyright":
                for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                    if k in data:
                        metrics["pyright"][k] = data.get(k)
                    elif isinstance(data.get("metrics"), dict) and k in data["metrics"]:
                        metrics["pyright"][k] = data["metrics"].get(k)

            if stage == "env_size":
                obs = data.get("observed") if isinstance(data, dict) else None
                if isinstance(obs, dict):
                    if "env_prefix_size_MB" in obs:
                        metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                    if "site_packages_total_bytes" in obs:
                        metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")

            if stage == "hallucination":
                h = data.get("hallucinations") if isinstance(data, dict) else None
                if isinstance(h, dict):
                    for kind in ("path", "version", "capability"):
                        if isinstance(h.get(kind), dict) and "count" in h[kind]:
                            metrics["hallucination"][f"{kind}_count"] = h[kind].get("count")

        overall_status = "failure" if failed else "success"

        status = "success" if overall_status == "success" else "failure"
        exit_code = 0 if overall_status == "success" else 1

        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "summary",
            "task": "check",
            "command": "python benchmark_scripts/summarize_results.py",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": assets,
            "overall_status": overall_status,
            "failed_stages": failed,
            "skipped_stages": skipped,
            "stages": stages_summary,
            "metrics": metrics,
            "meta": {
                "python": sys.executable,
                "git_commit": git_commit(repo),
                "env_vars": {},
                "decision_reason": "Aggregate per-stage results.json files into a single summary.",
                "timestamp_utc": utcnow(),
            },
            "failure_category": "unknown" if overall_status == "success" else "runtime",
            "error_excerpt": "" if overall_status == "success" else (log_path.read_text(encoding="utf-8", errors="replace")[-5000:]),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
