#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Optional


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def read_json(path: pathlib.Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git_commit(root: pathlib.Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        return ""


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    with log_path.open("w", encoding="utf-8") as log:
        log.write("[summary] reading stage results\n")

    stages_out: dict[str, Any] = {}
    failed: list[str] = []
    skipped: list[str] = []

    for stage in STAGES:
        stage_dir = root / "build_output" / stage
        res_path = stage_dir / "results.json"
        log_file = stage_dir / "log.txt"

        data = read_json(res_path)
        if data is None:
            failure_category = "missing_stage_results" if not res_path.exists() else "invalid_json"
            data = {
                "status": "failure",
                "exit_code": 1,
                "stage": stage,
                "failure_category": failure_category,
                "command": "",
            }

        status = data.get("status")
        exit_code = data.get("exit_code")

        stages_out[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": data.get("failure_category"),
            "command": data.get("command"),
            "results_path": str(res_path),
            "log_path": str(log_file),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or int(exit_code or 0) == 1:
            failed.append(stage)

    overall_status = "failure" if failed else "success"
    status = overall_status
    exit_code = 1 if overall_status == "failure" else 0

    metrics: dict[str, Any] = {}
    # Pyright metrics
    pyright_res = read_json(root / "build_output" / "pyright" / "results.json") or {}
    if isinstance(pyright_res, dict):
        metrics["pyright"] = {
            "missing_packages_count": pyright_res.get("missing_packages_count") or pyright_res.get("metrics", {}).get("missing_packages_count"),
            "total_imported_packages_count": pyright_res.get("total_imported_packages_count") or pyright_res.get("metrics", {}).get("total_imported_packages_count"),
            "missing_package_ratio": pyright_res.get("missing_package_ratio") or pyright_res.get("metrics", {}).get("missing_package_ratio"),
        }
    # Env size metrics
    env_res = read_json(root / "build_output" / "env_size" / "results.json") or {}
    if isinstance(env_res, dict):
        obs = env_res.get("observed") or {}
        metrics["env_size"] = {
            "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
            "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
        }
    # Hallucination metrics
    hallu_res = read_json(root / "build_output" / "hallucination" / "results.json") or {}
    if isinstance(hallu_res, dict):
        h = hallu_res.get("hallucinations") or {}
        metrics["hallucination"] = {
            "path": (h.get("path") or {}).get("count"),
            "version": (h.get("version") or {}).get("count"),
            "capability": (h.get("capability") or {}).get("count"),
        }

    summary = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"{sys.executable} {pathlib.Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {
            "git_commit": git_commit(root),
            "timestamp_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        },
        "failure_category": "unknown" if overall_status == "success" else "unknown",
        "error_excerpt": "",
    }
    write_json(results_path, summary)

    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[summary] overall_status={overall_status}\n")
        log.write(f"[summary] failed_stages={failed}\n")
        log.write(f"[summary] skipped_stages={skipped}\n")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
