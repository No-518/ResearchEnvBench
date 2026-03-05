#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return "unknown"


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception as e:
        return None, f"read_error: {e}"
    try:
        parsed = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    return parsed, None


def main() -> int:
    stage_order = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]

    out_dir = REPO_ROOT / "build_output" / "summary"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg.rstrip() + "\n")

    stages: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    pyright_metrics = {"missing_packages_count": None, "total_imported_packages_count": None, "missing_package_ratio": None}
    env_size_metrics = {"env_prefix_size_MB": None, "site_packages_total_bytes": None}
    hallucination_metrics = {"path_hallucinations": None, "version_hallucinations": None, "capability_hallucinations": None}

    for stage in stage_order:
        stage_dir = REPO_ROOT / "build_output" / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"

        data, err = _load_json(stage_results_path)
        if err or not data:
            summary = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err if err in {"missing_stage_results", "invalid_json"} else "invalid_json",
                "command": "",
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
            stages[stage] = summary
            failed_stages.append(stage)
            continue

        status = data.get("status", "failure")
        exit_code = int(data.get("exit_code", 1) or 1)
        failure_category = data.get("failure_category", "unknown")
        command = data.get("command", "")

        summary = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(stage_results_path),
            "log_path": str(stage_log_path),
        }

        if stage == "pyright":
            mp = data.get("missing_packages_count")
            tp = data.get("total_imported_packages_count")
            ratio = data.get("missing_package_ratio")
            summary["missing_package_ratio"] = ratio
            pyright_metrics = {
                "missing_packages_count": mp,
                "total_imported_packages_count": tp,
                "missing_package_ratio": ratio,
            }
        if stage == "env_size":
            obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            env_prefix_size_mb = obs.get("env_prefix_size_MB")
            site_total = obs.get("site_packages_total_bytes")
            summary["env_prefix_size_MB"] = env_prefix_size_mb
            summary["site_packages_total_bytes"] = site_total
            env_size_metrics = {"env_prefix_size_MB": env_prefix_size_mb, "site_packages_total_bytes": site_total}
        if stage == "hallucination":
            h = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            p = h.get("path", {}) if isinstance(h.get("path"), dict) else {}
            v = h.get("version", {}) if isinstance(h.get("version"), dict) else {}
            c = h.get("capability", {}) if isinstance(h.get("capability"), dict) else {}
            summary["path_hallucinations"] = p.get("count")
            summary["version_hallucinations"] = v.get("count")
            summary["capability_hallucinations"] = c.get("count")
            hallucination_metrics = {
                "path_hallucinations": p.get("count"),
                "version_hallucinations": v.get("count"),
                "capability_hallucinations": c.get("count"),
            }

        stages[stage] = summary

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

    overall_status = "failure" if failed_stages else "success"

    payload = {
        "status": "failure" if overall_status == "failure" else "success",
        "skip_reason": "not_applicable",
        "exit_code": 1 if overall_status == "failure" else 0,
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages,
        "metrics": {
            "pyright": pyright_metrics,
            "env_size": env_size_metrics,
            "hallucination": hallucination_metrics,
        },
        "meta": {"git_commit": _git_commit(REPO_ROOT), "timestamp_utc": _utc_timestamp()},
        "failure_category": "unknown" if overall_status == "success" else "runtime",
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(json.dumps(payload, ensure_ascii=False, indent=2))

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
