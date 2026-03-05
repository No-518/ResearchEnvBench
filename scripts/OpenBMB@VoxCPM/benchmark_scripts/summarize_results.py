#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def get_git_commit(root: Path) -> str:
    git_head = root / ".git" / "HEAD"
    if not git_head.exists():
        return ""
    # Best-effort; avoid running git.
    try:
        head = git_head.read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = root / ".git" / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8", errors="replace").strip()
        return head
    except Exception:
        return ""


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_order = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]

    stages: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    for stage in stages_order:
        stage_dir = root / "build_output" / stage
        rpath = stage_dir / "results.json"
        lpath = stage_dir / "log.txt"

        data, err = load_json(rpath)
        if data is None:
            status = "failure"
            exit_code = 1
            failure_category = "missing_stage_results" if err == "missing" else "invalid_json"
            command = ""
        else:
            status = str(data.get("status", "failure"))
            try:
                exit_code = int(data.get("exit_code", 1))
            except Exception:
                exit_code = 1
            failure_category = str(data.get("failure_category", "unknown"))
            command = str(data.get("command", ""))

        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(rpath),
            "log_path": str(lpath),
        }

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

    overall_status = "failure" if failed_stages else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    # Pyright metrics (best-effort).
    pyright_data, _ = load_json(root / "build_output" / "pyright" / "results.json")
    if isinstance(pyright_data, dict):
        for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
            if k in pyright_data:
                metrics["pyright"][k] = pyright_data.get(k)

    # Env size metrics.
    env_data, _ = load_json(root / "build_output" / "env_size" / "results.json")
    if isinstance(env_data, dict):
        obs = env_data.get("observed", {}) if isinstance(env_data.get("observed"), dict) else {}
        if "env_prefix_size_MB" in obs:
            metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
        if "site_packages_total_bytes" in obs:
            metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")

    # Hallucination metrics.
    hall_data, _ = load_json(root / "build_output" / "hallucination" / "results.json")
    if isinstance(hall_data, dict):
        h = hall_data.get("hallucinations", {}) if isinstance(hall_data.get("hallucinations"), dict) else {}
        for cat in ("path", "version", "capability"):
            if isinstance(h.get(cat), dict) and "count" in h.get(cat, {}):
                metrics["hallucination"][f"{cat}_count"] = h[cat].get("count")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "validate",
        "command": f"python {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages,
        "metrics": metrics,
        "meta": {"git_commit": get_git_commit(root), "timestamp_utc": utc_now_iso()},
        "failure_category": "unknown" if status == "failure" else "unknown",
        "error_excerpt": "",
    }

    log_lines = [f"overall_status={overall_status}"]
    if failed_stages:
        log_lines.append("failed_stages=" + ",".join(failed_stages))
    if skipped_stages:
        log_lines.append("skipped_stages=" + ",".join(skipped_stages))
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
