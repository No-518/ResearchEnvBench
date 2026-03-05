#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


STAGES_IN_ORDER = [
    "pyright",
    "prepare",
    "cpu",
    "cuda",
    "single_gpu",
    "multi_gpu",
    "env_size",
    "hallucination",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=10)
            .strip()
        )
    except Exception:
        return ""


def safe_load_json(path: Path) -> Tuple[Dict[str, Any], str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return {}, "missing_stage_results"
    except Exception:
        return {}, "invalid_json"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_summary: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[summary] timestamp_utc={utc_timestamp()}\n")
        for stage in STAGES_IN_ORDER:
            stage_dir = root / "build_output" / stage
            stage_results_path = stage_dir / "results.json"
            stage_log_path = stage_dir / "log.txt"

            obj, err_kind = safe_load_json(stage_results_path)
            if err_kind:
                entry = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": err_kind,
                    "command": "",
                    "results_path": str(stage_results_path),
                    "log_path": str(stage_log_path),
                }
                stages_summary[stage] = entry
                failed.append(stage)
                logf.write(f"[summary] {stage}: {err_kind}\n")
                continue

            status = str(obj.get("status", "") or "failure")
            exit_code = int(obj.get("exit_code", 0) or 0)
            failure_category = str(obj.get("failure_category", "") or "")
            command = str(obj.get("command", "") or "")

            stages_summary[stage] = {
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

    overall_status = "failure" if failed else "success"
    status = "success" if overall_status == "success" else "failure"
    exit_code = 0 if overall_status == "success" else 1

    metrics: Dict[str, Any] = {}
    pyright = stages_summary.get("pyright", {})
    if isinstance(pyright, dict):
        try:
            obj, err = safe_load_json(root / "build_output" / "pyright" / "results.json")
            if not err:
                metrics["pyright"] = {
                    "missing_packages_count": obj.get("missing_packages_count", None),
                    "total_imported_packages_count": obj.get("total_imported_packages_count", None),
                    "missing_package_ratio": obj.get("missing_package_ratio", None),
                }
        except Exception:
            pass

    try:
        obj, err = safe_load_json(root / "build_output" / "env_size" / "results.json")
        if not err and isinstance(obj.get("observed"), dict):
            obs = obj["observed"]
            metrics["env_size"] = {
                "env_prefix_size_MB": obs.get("env_prefix_size_MB", None),
                "site_packages_total_bytes": obs.get("site_packages_total_bytes", None),
            }
    except Exception:
        pass

    try:
        obj, err = safe_load_json(root / "build_output" / "hallucination" / "results.json")
        if not err and isinstance(obj.get("hallucinations"), dict):
            h = obj["hallucinations"]
            metrics["hallucination"] = {
                "path_hallucinations": (h.get("path", {}) or {}).get("count", None),
                "version_hallucinations": (h.get("version", {}) or {}).get("count", None),
                "capability_hallucinations": (h.get("capability", {}) or {}).get("count", None),
            }
    except Exception:
        pass

    try:
        error_excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-240:]).strip()
    except Exception:
        error_excerpt = ""

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
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
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
        "failure_category": "" if exit_code == 0 else "unknown",
        "error_excerpt": error_excerpt,
    }
    results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
