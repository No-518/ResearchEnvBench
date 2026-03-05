#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


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


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def git_commit(root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:  # noqa: BLE001
        return None, "invalid_json"


def main(argv: list[str] | None = None) -> int:
    out_base = "build_output"

    root = repo_root()
    summary_dir = root / out_base / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    log_path = summary_dir / "log.txt"
    results_path = summary_dir / "results.json"

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg)

    stages_summary: dict[str, Any] = {}
    failed_stages: list[str] = []
    skipped_stages: list[str] = []

    for stage in STAGE_ORDER:
        stage_dir = root / out_base / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"

        data, err = load_json(stage_results_path)
        if data is None:
            stage_info = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
        else:
            raw_exit = data.get("exit_code", 1)
            try:
                exit_code = int(raw_exit)
            except Exception:  # noqa: BLE001
                exit_code = 1
            stage_info = {
                "status": data.get("status", "failure"),
                "exit_code": exit_code,
                "failure_category": data.get("failure_category", "unknown"),
                "command": data.get("command", ""),
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }

        stages_summary[stage] = stage_info
        if stage_info["status"] == "skipped":
            skipped_stages.append(stage)
        elif stage_info["status"] == "failure" or stage_info["exit_code"] == 1:
            failed_stages.append(stage)

        log(f"{stage}: status={stage_info['status']} exit_code={stage_info['exit_code']}")

    overall_status = "failure" if failed_stages else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    # Pyright metrics
    pyright_res, _ = load_json(root / out_base / "pyright" / "results.json")
    if isinstance(pyright_res, dict):
        m = pyright_res.get("metrics", pyright_res)
        if isinstance(m, dict):
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in m:
                    metrics["pyright"][k] = m.get(k)

    # Env size metrics
    env_res, _ = load_json(root / out_base / "env_size" / "results.json")
    if isinstance(env_res, dict):
        obs = env_res.get("observed", {})
        if isinstance(obs, dict):
            if "env_prefix_size_MB" in obs:
                metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in obs:
                metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")

    # Hallucination metrics
    hall_res, _ = load_json(root / out_base / "hallucination" / "results.json")
    if isinstance(hall_res, dict):
        h = hall_res.get("hallucinations", {})
        if isinstance(h, dict):
            for cat in ("path", "version", "capability"):
                c = h.get(cat, {})
                if isinstance(c, dict) and "count" in c:
                    metrics["hallucination"][f"{cat}_count"] = c.get("count")

    summary = {
        "status": status,
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {
            "git_commit": git_commit(root),
            "timestamp_utc": now_utc_iso(),
        },
        "failure_category": "unknown" if overall_status == "success" else "runtime",
    }

    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
