#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGE_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main(argv: List[str]) -> int:
    repo_root = _repo_root()
    build_root = repo_root / "build_output"
    out_dir = build_root / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    failed: List[str] = []
    skipped: List[str] = []
    stages_out: Dict[str, Any] = {}

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGE_ORDER:
        stage_dir = build_root / stage
        stage_results = stage_dir / "results.json"
        stage_log = stage_dir / "log.txt"

        if not stage_results.exists():
            entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_stage_results",
                "command": "",
                "results_path": str(stage_results),
                "log_path": str(stage_log),
            }
            failed.append(stage)
            stages_out[stage] = entry
            continue

        data, err = _read_json(stage_results)
        if data is None:
            entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "invalid_json",
                "command": "",
                "results_path": str(stage_results),
                "log_path": str(stage_log),
                "error": err,
            }
            failed.append(stage)
            stages_out[stage] = entry
            continue

        status = str(data.get("status", "failure"))
        exit_code = int(data.get("exit_code", 1)) if str(data.get("exit_code", "")).isdigit() else int(data.get("exit_code", 1) or 1)
        failure_category = str(data.get("failure_category", "unknown"))
        command = str(data.get("command", ""))

        entry = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(stage_results),
            "log_path": str(stage_log),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        stages_out[stage] = entry

        # Metrics aggregation
        if stage == "pyright":
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in data:
                    metrics["pyright"][k] = data.get(k)
        elif stage == "env_size":
            observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            if "env_prefix_size_MB" in observed:
                metrics["env_size"]["env_prefix_size_MB"] = observed.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in observed:
                metrics["env_size"]["site_packages_total_bytes"] = observed.get("site_packages_total_bytes")
        elif stage == "hallucination":
            halluc = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            for k in ("path", "version", "capability"):
                section = halluc.get(k) if isinstance(halluc.get(k), dict) else {}
                if "count" in section:
                    metrics["hallucination"][f"{k}_count"] = section.get("count")

    overall_status = "failure" if failed else "success"
    exit_code = 1 if overall_status == "failure" else 0

    meta: Dict[str, Any] = {"timestamp_utc": _utc_now_iso(), "git_commit": None}
    try:
        meta["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
    except Exception:
        meta["git_commit"] = None

    summary = {
        "stage": "summary",
        "status": "failure" if exit_code != 0 else "success",
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "summarize",
        "command": "python benchmark_scripts/summarize_results.py",
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
        "meta": meta,
        "failure_category": "unknown" if exit_code == 0 else "runtime",
        "error_excerpt": "",
    }

    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log_lines = [f"[summary] timestamp_utc={meta['timestamp_utc']}", f"[summary] overall_status={overall_status}"]
    if failed:
        log_lines.append("[summary] failed_stages=" + ", ".join(failed))
    if skipped:
        log_lines.append("[summary] skipped_stages=" + ", ".join(skipped))
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
