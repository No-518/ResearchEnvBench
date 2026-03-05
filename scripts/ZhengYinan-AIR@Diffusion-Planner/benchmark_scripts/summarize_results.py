#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root()), text=True).strip()
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    if not path.exists():
        return None, "missing_stage_results"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"invalid_json: {e}"


def stage_result_stub(stage: str, failure_category: str, results_path: Path) -> Dict[str, Any]:
    return {
        "status": "failure",
        "exit_code": 1,
        "failure_category": failure_category,
        "command": "",
        "results_path": str(results_path),
        "log_path": str(results_path.parent / "log.txt"),
        "stage": stage,
    }


def extract_metrics(stage_data: Dict[str, Any], stage: str) -> Dict[str, Any]:
    if stage == "env_size":
        obs = stage_data.get("observed") if isinstance(stage_data.get("observed"), dict) else {}
        return {
            "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
            "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
        }
    if stage == "hallucination":
        h = stage_data.get("hallucinations") if isinstance(stage_data.get("hallucinations"), dict) else {}
        return {
            "path_hallucinations": (h.get("path", {}) or {}).get("count"),
            "version_hallucinations": (h.get("version", {}) or {}).get("count"),
            "capability_hallucinations": (h.get("capability", {}) or {}).get("count"),
        }
    if stage == "pyright":
        # Prefer metrics at top-level (if present), else fall back to analysis.json.
        keys = ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]
        if all(k in stage_data for k in keys):
            return {k: stage_data.get(k) for k in keys}
        analysis_path = repo_root() / "build_output" / "pyright" / "analysis.json"
        analysis, err = load_json(analysis_path)
        if not err and isinstance(analysis, dict) and isinstance(analysis.get("metrics"), dict):
            m = analysis["metrics"]
            return {k: m.get(k) for k in keys}
    return {}


def main() -> int:
    out_dir = repo_root() / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_lines: List[str] = []
    log_lines.append(f"[summary] timestamp_utc={utc_now_iso()}")

    stages_out: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES:
        rp = repo_root() / "build_output" / stage / "results.json"
        data, err = load_json(rp)
        if err:
            fc = "missing_stage_results" if err == "missing_stage_results" else "invalid_json"
            stages_out[stage] = stage_result_stub(stage, fc, rp)
            failed_stages.append(stage)
            log_lines.append(f"[summary] {stage}: {fc}")
            continue

        if not isinstance(data, dict):
            stages_out[stage] = stage_result_stub(stage, "invalid_json", rp)
            failed_stages.append(stage)
            log_lines.append(f"[summary] {stage}: invalid_json (not an object)")
            continue

        status = data.get("status", "")
        exit_code = int(data.get("exit_code", 1)) if str(data.get("exit_code", "")).isdigit() else 1
        fc = data.get("failure_category", "unknown")
        cmd = data.get("command", "")

        stages_out[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": fc,
            "command": cmd,
            "results_path": str(rp),
            "log_path": str(rp.parent / "log.txt"),
            "stage": stage,
        }

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

        if stage in metrics:
            metrics[stage] = extract_metrics(data, stage)

    overall_status = "failure" if failed_stages else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    summary: Dict[str, Any] = {
        "stage": "summary",
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "summarize",
        "command": "python benchmark_scripts/summarize_results.py",
        "timeout_sec": 120,
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(), "timestamp_utc": utc_now_iso()},
        "failure_category": "unknown" if overall_status == "success" else "unknown",
        "error_excerpt": "",
    }

    results_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
