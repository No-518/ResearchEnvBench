#!/usr/bin/env python3
from __future__ import annotations

import json
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


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json_dict(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, ""
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def tail_lines(path: Path, max_lines: int = 120) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def read_git_commit() -> str:
    git_head = REPO_ROOT / ".git" / "HEAD"
    if not git_head.exists():
        return ""
    try:
        head = git_head.read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            ref_path = REPO_ROOT / ".git" / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()
        return head
    except Exception:
        return ""


def main(argv: List[str]) -> int:
    out_dir = REPO_ROOT / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    failed_stages: List[str] = []
    skipped_stages: List[str] = []
    stages_out: Dict[str, Any] = {}

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES_ORDER:
        results_p = REPO_ROOT / "build_output" / stage / "results.json"
        log_p = REPO_ROOT / "build_output" / stage / "log.txt"
        data, err = load_json_dict(results_p)
        if data is None:
            stage_entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(results_p),
                "log_path": str(log_p),
            }
            stages_out[stage] = stage_entry
            failed_stages.append(stage)
            continue

        status = str(data.get("status") or "")
        exit_code = int(data.get("exit_code") or 0) if str(data.get("exit_code") or "").strip() else 0
        failure_category = str(data.get("failure_category") or "")
        command = str(data.get("command") or "")

        stages_out[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(results_p),
            "log_path": str(log_p),
        }

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

        if stage == "pyright":
            for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
                if k in data:
                    metrics["pyright"][k] = data.get(k)
        if stage == "env_size":
            obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            if isinstance(obs, dict):
                if "env_prefix_size_MB" in obs:
                    metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                if "site_packages_total_bytes" in obs:
                    metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
        if stage == "hallucination":
            hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            if isinstance(hall, dict):
                for k in ["path", "version", "capability"]:
                    if isinstance(hall.get(k), dict) and "count" in hall[k]:
                        metrics["hallucination"][f"{k}_count"] = hall[k].get("count")

    overall_status = "failure" if failed_stages else "success"

    summary: Dict[str, Any] = {
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {
            "git_commit": read_git_commit(),
            "timestamp_utc": now_utc_iso(),
        },
    }

    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_path.write_text(tail_lines(results_path), encoding="utf-8")
    return 0 if overall_status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

