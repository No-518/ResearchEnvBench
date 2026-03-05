#!/usr/bin/env python3
from __future__ import annotations

import json
import os
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def _git_commit(repo_root: Path) -> str:
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out
    except Exception:
        return ""


def main(argv: Optional[list[str]] = None) -> int:
    _ = argv
    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "summary"
    _ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    stages_out: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[summary] timestamp_utc={_utc_timestamp()}\n")
        for stage in STAGE_ORDER:
            res_path = repo_root / "build_output" / stage / "results.json"
            stage_log_path = repo_root / "build_output" / stage / "log.txt"

            data, err = _safe_json_load(res_path)
            if err is not None:
                stages_out[stage] = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": err,
                    "command": "",
                    "results_path": str(res_path),
                    "log_path": str(stage_log_path),
                }
                failed.append(stage)
                log_f.write(f"[summary] {stage}: {err}\n")
                continue

            assert isinstance(data, dict)
            status = str(data.get("status") or "failure")
            exit_code = int(data.get("exit_code") or 0)
            failure_category = str(data.get("failure_category") or "unknown")
            command = str(data.get("command") or "")

            stages_out[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(res_path),
                "log_path": str(stage_log_path),
            }

            if status == "skipped":
                skipped.append(stage)
            elif status == "failure" or exit_code == 1:
                failed.append(stage)

            if stage == "pyright":
                metrics["pyright"] = {
                    "missing_packages_count": data.get("missing_packages_count"),
                    "total_imported_packages_count": data.get("total_imported_packages_count"),
                    "missing_package_ratio": data.get("missing_package_ratio"),
                }
            elif stage == "env_size":
                obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
                metrics["env_size"] = {
                    "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                    "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                }
            elif stage == "hallucination":
                h = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
                def _count(kind: str) -> Any:
                    v = h.get(kind)
                    return v.get("count") if isinstance(v, dict) else None
                metrics["hallucination"] = {
                    "path_hallucinations": _count("path"),
                    "version_hallucinations": _count("version"),
                    "capability_hallucinations": _count("capability"),
                }

    overall_status = "failure" if failed else "success"
    exit_code = 1 if overall_status == "failure" else 0
    status = "failure" if exit_code == 1 else "success"
    payload: Dict[str, Any] = {
        "stage": "summary",
        "status": status,
        "exit_code": exit_code,
        "task": "summarize",
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_timestamp(),
        },
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
