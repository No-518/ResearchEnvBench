#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


STAGE_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def as_int_exit_code(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.DEVNULL, text=True).strip()
        return out
    except Exception:
        return ""


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_summary: dict[str, Any] = {}
    failed: list[str] = []
    skipped: list[str] = []

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[summary] timestamp_utc={utc_now_iso()}\n")
            for stage in STAGE_ORDER:
                stage_dir = root / "build_output" / stage
                rp = stage_dir / "results.json"
                lp = stage_dir / "log.txt"

                data = read_json(rp) if rp.is_file() else None
                if data is None:
                    status = "failure"
                    exit_code = 1
                    failure_category = "missing_stage_results" if not rp.is_file() else "invalid_json"
                    cmd = ""
                else:
                    status = str(data.get("status", "failure"))
                    exit_code = as_int_exit_code(data.get("exit_code", 1), default=1)
                    failure_category = str(data.get("failure_category", ""))
                    cmd = str(data.get("command", ""))

                stages_summary[stage] = {
                    "status": status,
                    "exit_code": exit_code,
                    "failure_category": failure_category,
                    "command": cmd,
                    "results_path": str(rp),
                    "log_path": str(lp),
                }

                if status == "skipped":
                    skipped.append(stage)
                elif status == "failure" or exit_code == 1:
                    failed.append(stage)

                # Aggregate stage metrics where available.
                if stage == "pyright" and data:
                    m = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
                    metrics["pyright"] = {
                        "missing_packages_count": data.get("missing_packages_count", m.get("missing_packages_count")),
                        "total_imported_packages_count": data.get(
                            "total_imported_packages_count", m.get("total_imported_packages_count")
                        ),
                        "missing_package_ratio": data.get("missing_package_ratio", m.get("missing_package_ratio")),
                    }
                if stage == "env_size" and data:
                    obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
                    metrics["env_size"] = {
                        "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                        "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                    }
                if stage == "hallucination" and data:
                    hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
                    def _count(k: str) -> int:
                        v = hall.get(k)
                        if isinstance(v, dict):
                            try:
                                return int(v.get("count", 0) or 0)
                            except Exception:
                                return 0
                        return 0
                    metrics["hallucination"] = {
                        "path_hallucinations": _count("path"),
                        "version_hallucinations": _count("version"),
                        "capability_hallucinations": _count("capability"),
                    }

                lf.write(f"[summary] {stage}: status={status} exit_code={exit_code} failure_category={failure_category}\n")

        overall_status = "failure" if failed else "success"
        status = "failure" if overall_status == "failure" else "success"
        exit_code = 1 if overall_status == "failure" else 0
        payload: dict[str, Any] = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "summary",
            "task": "summarize",
            "command": "benchmark_scripts/summarize_results.py",
            "overall_status": overall_status,
            "failed_stages": failed,
            "skipped_stages": skipped,
            "stages": stages_summary,
            "metrics": metrics,
            "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_now_iso()},
            "failure_category": "" if overall_status == "success" else "unknown",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return exit_code
    except Exception:
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n[summary] exception:\n")
            lf.write(traceback.format_exc())
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "summary",
            "task": "summarize",
            "command": "benchmark_scripts/summarize_results.py",
            "overall_status": "failure",
            "failed_stages": STAGE_ORDER,
            "skipped_stages": [],
            "stages": stages_summary,
            "metrics": metrics,
            "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_now_iso()},
            "failure_category": "runtime",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
