#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, capture_output=True, check=False)
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _parse_exit_code(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def read_stage(stage: str) -> dict[str, Any]:
        stage_dir = repo_root / "build_output" / stage
        res_path = stage_dir / "results.json"
        log_p = stage_dir / "log.txt"
        if not res_path.exists():
            return {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_stage_results",
                "command": "",
                "results_path": str(res_path),
                "log_path": str(log_p),
            }
        try:
            data = json.loads(res_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "invalid_json",
                "command": "",
                "results_path": str(res_path),
                "log_path": str(log_p),
            }
        return {
            "status": data.get("status", "failure"),
            "exit_code": _parse_exit_code(data.get("exit_code", 1), default=1),
            "failure_category": data.get("failure_category", "unknown"),
            "command": data.get("command", ""),
            "results_path": str(res_path),
            "log_path": str(log_p),
            "_raw": data,
        }

    stages = {s: read_stage(s) for s in STAGES}

    failed = [s for s in STAGES if stages[s]["status"] == "failure" or stages[s]["exit_code"] == 1]
    skipped = [s for s in STAGES if stages[s]["status"] == "skipped"]
    overall_status = "failure" if failed else "success"
    status = "success" if overall_status == "success" else "failure"
    exit_code = 0 if status == "success" else 1

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    pr = stages["pyright"].get("_raw") or {}
    if isinstance(pr, dict):
        metrics["pyright"] = {
            "missing_packages_count": pr.get("missing_packages_count"),
            "total_imported_packages_count": pr.get("total_imported_packages_count"),
            "missing_package_ratio": pr.get("missing_package_ratio"),
        }

    es = stages["env_size"].get("_raw") or {}
    if isinstance(es, dict):
        observed = es.get("observed", {}) if isinstance(es.get("observed", {}), dict) else {}
        metrics["env_size"] = {
            "env_prefix_size_MB": observed.get("env_prefix_size_MB"),
            "site_packages_total_bytes": observed.get("site_packages_total_bytes"),
        }

    ha = stages["hallucination"].get("_raw") or {}
    if isinstance(ha, dict):
        halluc = ha.get("hallucinations", {}) if isinstance(ha.get("hallucinations", {}), dict) else {}
        metrics["hallucination"] = {
            "path_hallucinations": (halluc.get("path", {}) or {}).get("count"),
            "version_hallucinations": (halluc.get("version", {}) or {}).get("count"),
            "capability_hallucinations": (halluc.get("capability", {}) or {}).get("count"),
        }

    summary: dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"python {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": {
            k: {kk: vv for kk, vv in v.items() if kk != "_raw"} for k, v in stages.items()
        },
        "metrics": metrics,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp()},
        "failure_category": "not_applicable" if exit_code == 0 else "unknown",
        "error_excerpt": "" if exit_code == 0 else f"failed_stages={failed}",
    }

    log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_json(results_path, summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
