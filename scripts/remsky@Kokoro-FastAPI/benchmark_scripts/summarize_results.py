#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_git_commit(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def safe_load_json(path: Path) -> Tuple[Optional[dict], str]:
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
        return None, "invalid_json"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stage_order = [
        "pyright",
        "prepare",
        "cpu",
        "cuda",
        "single_gpu",
        "multi_gpu",
        "env_size",
        "hallucination",
    ]

    logs: List[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        print(msg)

    stages: Dict[str, Dict[str, Any]] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    for stage in stage_order:
        stage_dir = root / "build_output" / stage
        rpath = stage_dir / "results.json"
        lpath = stage_dir / "log.txt"

        data, err = safe_load_json(rpath)
        if data is None:
            entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(rpath),
                "log_path": str(lpath),
            }
            stages[stage] = entry
            failed_stages.append(stage)
            log(f"{stage}: {err}")
            continue

        status = str(data.get("status") or "unknown")
        exit_code = int(data.get("exit_code") or 0)
        failure_category = str(data.get("failure_category") or "")
        command = str(data.get("command") or "")

        entry = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(rpath),
            "log_path": str(lpath),
        }
        stages[stage] = entry

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

        log(f"{stage}: status={status} exit_code={exit_code} failure_category={failure_category}")

    overall_status = "failure" if failed_stages else "success"
    summary_status = "failure" if overall_status == "failure" else "success"
    summary_exit_code = 1 if overall_status == "failure" else 0

    # Aggregate metrics when available.
    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    pyright_data, _ = safe_load_json(root / "build_output" / "pyright" / "results.json")
    if isinstance(pyright_data, dict):
        for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
            if k in pyright_data:
                metrics["pyright"][k] = pyright_data.get(k)

    env_size_data, _ = safe_load_json(root / "build_output" / "env_size" / "results.json")
    if isinstance(env_size_data, dict):
        obs = env_size_data.get("observed") or {}
        if isinstance(obs, dict):
            if "env_prefix_size_MB" in obs:
                metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in obs:
                metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")

    hall_data, _ = safe_load_json(root / "build_output" / "hallucination" / "results.json")
    if isinstance(hall_data, dict):
        h = hall_data.get("hallucinations") or {}
        if isinstance(h, dict):
            for key in ["path", "version", "capability"]:
                if isinstance(h.get(key), dict) and "count" in h[key]:
                    metrics["hallucination"][f"{key}_hallucinations"] = h[key]["count"]

    summary_payload: Dict[str, Any] = {
        "status": summary_status,
        "skip_reason": "unknown",
        "exit_code": summary_exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"{sys.executable} {Path(__file__).name}",
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
            "git_commit": get_git_commit(root),
            "timestamp_utc": utc_timestamp(),
        },
        "failure_category": "unknown" if overall_status == "failure" else "",
        "error_excerpt": "\n".join(logs[-200:]),
    }

    log_path.write_text("\n".join(logs) + ("\n" if logs else ""), encoding="utf-8")
    write_json(results_path, summary_payload)

    return summary_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
