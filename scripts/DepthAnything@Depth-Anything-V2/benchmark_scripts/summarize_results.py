#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGE_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def try_git_commit(root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def safe_load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, "missing_stage_results"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception:
        return None, "invalid_json"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[summary] timestamp_utc={utc_now_iso()}\n")

        for stage in STAGE_ORDER:
            stage_dir = root / "build_output" / stage
            res_path = stage_dir / "results.json"
            log_f.write(f"[summary] reading {res_path}\n")

            data, err = safe_load_json(res_path)
            if data is None:
                stages[stage] = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": err,
                    "command": "",
                    "results_path": str(res_path),
                    "log_path": str(stage_dir / "log.txt"),
                }
                failed_stages.append(stage)
                continue

            status = str(data.get("status", "failure"))
            try:
                exit_code = int(data.get("exit_code", 1))
            except Exception:
                exit_code = 1
            failure_category = str(data.get("failure_category", ""))
            command = str(data.get("command", ""))

            stages[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(res_path),
                "log_path": str(stage_dir / "log.txt"),
            }

            if status == "skipped":
                skipped_stages.append(stage)
            elif status == "failure" or exit_code == 1:
                failed_stages.append(stage)

            if stage == "pyright":
                m = data.get("metrics", {})
                if isinstance(m, dict):
                    for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                        if k in m:
                            metrics["pyright"][k] = m.get(k)
            elif stage == "env_size":
                obs = data.get("observed", {})
                if isinstance(obs, dict):
                    if "env_prefix_size_MB" in obs:
                        metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                    if "site_packages_total_bytes" in obs:
                        metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
            elif stage == "hallucination":
                h = data.get("hallucinations", {})
                if isinstance(h, dict):
                    for k in ("path", "version", "capability"):
                        metrics["hallucination"][f"{k}_count"] = (h.get(k, {}) or {}).get("count", 0)

    overall_status = "success"
    if failed_stages:
        overall_status = "failure"

    status = overall_status
    exit_code = 1 if overall_status == "failure" else 0

    payload = {
        "stage": "summary",
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "task": "validate",
        "command": f"{sys.executable} benchmark_scripts/summarize_results.py",
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
        "meta": {
            "git_commit": try_git_commit(root),
            "timestamp_utc": utc_now_iso(),
        },
        "failure_category": "unknown" if overall_status == "failure" else "",
        "error_excerpt": log_path.read_text(encoding="utf-8", errors="replace")[-8000:],
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
