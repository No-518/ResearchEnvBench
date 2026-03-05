#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_text(path: Path, max_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(f, maxlen=max_lines)
        return "".join(dq).strip()
    except Exception:
        return ""


def safe_load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "invalid_json"


def parse_exit_code(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def git_commit(repo_root: Path) -> str | None:
    try:
        import subprocess

        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None


def main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started_utc = utc_now()
    failed: list[str] = []
    skipped: list[str] = []

    stages_out: dict[str, Any] = {}
    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[summary] started_utc={started_utc}\n")

        for stage in STAGES:
            stage_dir = repo_root / "build_output" / stage
            res_path = stage_dir / "results.json"
            lg_path = stage_dir / "log.txt"
            data, err = safe_load_json(res_path)
            if data is None:
                stage_entry = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": err,
                    "command": "",
                    "results_path": str(res_path),
                    "log_path": str(lg_path),
                }
                failed.append(stage)
                stages_out[stage] = stage_entry
                log.write(f"[summary] {stage}: {err}\n")
                continue

            status = data.get("status", "failure")
            exit_code = parse_exit_code(data.get("exit_code", 1), default=1)
            failure_category = data.get("failure_category", "")
            stages_out[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": data.get("command", ""),
                "results_path": str(res_path),
                "log_path": str(lg_path),
            }

            if status == "skipped":
                skipped.append(stage)
            if status == "failure" or exit_code != 0:
                failed.append(stage)

            if stage == "pyright":
                m = data.get("metrics") or {}
                if isinstance(m, dict):
                    metrics["pyright"] = {
                        "missing_packages_count": m.get("missing_packages_count"),
                        "total_imported_packages_count": m.get("total_imported_packages_count"),
                        "missing_package_ratio": m.get("missing_package_ratio"),
                    }
            if stage == "env_size":
                obs = data.get("observed") or {}
                if isinstance(obs, dict):
                    metrics["env_size"] = {
                        "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                        "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                    }
            if stage == "hallucination":
                h = data.get("hallucinations") or {}
                if isinstance(h, dict):
                    metrics["hallucination"] = {
                        "path": (h.get("path") or {}).get("count"),
                        "version": (h.get("version") or {}).get("count"),
                        "capability": (h.get("capability") or {}).get("count"),
                    }

        overall_status = "failure" if any(s in failed for s in STAGES) else "success"
        status = "success" if overall_status == "success" else "failure"
        exit_code = 0 if status == "success" else 1

        payload: dict[str, Any] = {
            "status": status,
            "exit_code": exit_code,
            "stage": "summary",
            "task": "summarize",
            "command": "python summarize_results.py",
            "timeout_sec": 120,
            "overall_status": overall_status,
            "failed_stages": failed,
            "skipped_stages": skipped,
            "stages": stages_out,
            "metrics": metrics,
            "meta": {
                "git_commit": git_commit(repo_root),
                "timestamp_utc": utc_now(),
            },
            "failure_category": ("unknown" if status == "failure" else ""),
            "error_excerpt": "",
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.write(f"[summary] finished_utc={utc_now()}\n")
        if failed:
            log.write(f"[summary] failed_stages={failed}\n")

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
