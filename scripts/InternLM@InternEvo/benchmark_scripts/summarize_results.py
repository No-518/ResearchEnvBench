#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        txt = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "invalid_json"
    try:
        obj = json.loads(txt)
    except Exception:
        return None, "invalid_json"
    if not isinstance(obj, dict):
        return None, "invalid_json"
    return obj, None


def _stage_paths(repo_root: Path, stage: str) -> Tuple[Path, Path]:
    stage_dir = repo_root / "build_output" / stage
    return stage_dir / "results.json", stage_dir / "log.txt"


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        msg = f"[{_utc_now_iso()}] {line}"
        print(msg)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    log_path.write_text("", encoding="utf-8")

    stage_summaries: Dict[str, Dict[str, Any]] = {}
    failed: List[str] = []
    skipped: List[str] = []

    # Metrics aggregation (best-effort)
    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES:
        res_path, log_txt = _stage_paths(repo_root, stage)
        obj, err_cat = _read_json(res_path)
        if obj is None:
            stage_obj = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err_cat,
                "command": "",
            }
        else:
            stage_obj = obj

        status = str(stage_obj.get("status", "failure"))
        raw_exit = stage_obj.get("exit_code", 1)
        exit_code = int(raw_exit) if raw_exit is not None else 1
        failure_category = str(stage_obj.get("failure_category", "unknown"))
        command = str(stage_obj.get("command", ""))

        stage_summaries[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(res_path),
            "log_path": str(log_txt),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        if stage == "pyright" and obj is not None:
            m = stage_obj.get("metrics") if isinstance(stage_obj.get("metrics"), dict) else {}
            if m:
                metrics["pyright"] = {
                    "missing_packages_count": m.get("missing_packages_count"),
                    "total_imported_packages_count": m.get("total_imported_packages_count"),
                    "missing_package_ratio": m.get("missing_package_ratio"),
                }
        if stage == "env_size" and obj is not None:
            obs = stage_obj.get("observed") if isinstance(stage_obj.get("observed"), dict) else {}
            if obs:
                metrics["env_size"] = {
                    "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                    "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                }
        if stage == "hallucination" and obj is not None:
            hall = stage_obj.get("hallucinations") if isinstance(stage_obj.get("hallucinations"), dict) else {}
            if hall:
                metrics["hallucination"] = {
                    "path_hallucinations": hall.get("path", {}).get("count"),
                    "version_hallucinations": hall.get("version", {}).get("count"),
                    "capability_hallucinations": hall.get("capability", {}).get("count"),
                }

    overall_status = "failure" if failed else "success"
    summary = {
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stage_summaries,
        "metrics": metrics,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "python": f"{sys.executable} ({platform.python_version()})",
        },
    }

    results_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"overall_status={overall_status} failed={failed} skipped={skipped}")
    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
