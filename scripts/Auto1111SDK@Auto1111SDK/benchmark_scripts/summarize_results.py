#!/usr/bin/env python3
from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGES_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception as e:
        return None, f"read_error: {type(e).__name__}: {e}"
    try:
        obj = json.loads(raw)
    except Exception as e:
        return None, f"invalid_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_json: top-level is not an object"
    return obj, None


def main() -> int:
    out_dir = REPO_ROOT / "build_output" / "summary"
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    log(f"[summary] start_utc={_utc_now_iso()}")

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {
        "pyright": {},
        "env_size": {},
        "hallucination": {},
    }

    for stage in STAGES_ORDER:
        stage_dir = REPO_ROOT / "build_output" / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"

        data, err = _read_json(stage_results_path)
        if err:
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_stage_results" if err == "missing_stage_results" else "invalid_json",
                "command": "",
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
                "error": err,
            }
            failed.append(stage)
            continue

        status = data.get("status", "failure")
        exit_code = data.get("exit_code", 1)
        failure_category = data.get("failure_category", "unknown")
        command = data.get("command", "")
        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(stage_results_path),
            "log_path": str(stage_log_path),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        if stage == "pyright":
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in data:
                    metrics["pyright"][k] = data.get(k)
        if stage == "env_size":
            obs = data.get("observed", {}) if isinstance(data.get("observed"), dict) else {}
            if "env_prefix_size_MB" in obs:
                metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in obs:
                metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
        if stage == "hallucination":
            halluc = data.get("hallucinations", {}) if isinstance(data.get("hallucinations"), dict) else {}
            for key in ("path", "version", "capability"):
                sub = halluc.get(key, {}) if isinstance(halluc.get(key), dict) else {}
                if "count" in sub:
                    metrics["hallucination"][f"{key}_count"] = sub.get("count")

    overall_status = "failure" if failed else "success"
    status = "success" if overall_status == "success" else "failure"
    exit_code = 0 if status == "success" else 1

    base_assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": base_assets,
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(REPO_ROOT),
            "timestamp_utc": _utc_now_iso(),
            "decision_reason": "Aggregate per-stage results into a single summary.",
        },
        "failure_category": "unknown" if exit_code == 0 else "missing_stage_results",
        "error_excerpt": "" if exit_code == 0 else "one or more stages failed or results were missing/invalid",
    }
    _write_json(results_path, payload)

    if overall_status == "failure":
        log(f"[summary] FAILED stages: {failed}")
        return 1
    log("[summary] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
