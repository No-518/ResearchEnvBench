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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit(repo: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:])


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    repo = _repo_root()
    out_dir = repo / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"timestamp_utc={_utc_timestamp()}\n")
        logf.write("reading stages: " + ", ".join(STAGE_ORDER) + "\n")

    stages: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    skipped: list[str] = []

    for stage in STAGE_ORDER:
        stage_dir = repo / "build_output" / stage
        res_path = stage_dir / "results.json"
        lg_path = stage_dir / "log.txt"

        if not res_path.exists():
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_stage_results",
                "command": "",
                "results_path": str(res_path),
                "log_path": str(lg_path),
            }
            failed.append(stage)
            continue

        try:
            data = _read_json(res_path)
            if not isinstance(data, dict):
                raise ValueError("results.json is not an object")
        except Exception:
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "invalid_json",
                "command": "",
                "results_path": str(res_path),
                "log_path": str(lg_path),
            }
            failed.append(stage)
            continue

        st = str(data.get("status", "failure"))
        ec = int(data.get("exit_code", 1))
        fc = str(data.get("failure_category", "unknown"))
        cmd = str(data.get("command", ""))

        stages[stage] = {
            "status": st,
            "exit_code": ec,
            "failure_category": fc,
            "command": cmd,
            "results_path": str(res_path),
            "log_path": str(lg_path),
        }

        if st == "skipped":
            skipped.append(stage)
        elif st == "failure" or ec == 1:
            failed.append(stage)

    overall_status = "failure" if failed else "success"
    exit_code = 1 if overall_status == "failure" else 0

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    # Pyright metrics
    try:
        pr = _read_json(repo / "build_output" / "pyright" / "results.json")
        metrics["pyright"] = {
            "missing_packages_count": pr.get("missing_packages_count"),
            "total_imported_packages_count": pr.get("total_imported_packages_count"),
            "missing_package_ratio": pr.get("missing_package_ratio"),
        }
    except Exception:
        pass

    # Env size metrics
    try:
        es = _read_json(repo / "build_output" / "env_size" / "results.json")
        obs = es.get("observed", {}) if isinstance(es, dict) else {}
        metrics["env_size"] = {
            "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
            "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
        }
    except Exception:
        pass

    # Hallucination metrics
    try:
        h = _read_json(repo / "build_output" / "hallucination" / "results.json")
        hs = h.get("hallucinations", {}) if isinstance(h, dict) else {}
        metrics["hallucination"] = {
            "path_hallucinations": (hs.get("path", {}) or {}).get("count"),
            "version_hallucinations": (hs.get("version", {}) or {}).get("count"),
            "capability_hallucinations": (hs.get("capability", {}) or {}).get("count"),
        }
    except Exception:
        pass

    payload: dict[str, Any] = {
        "status": "failure" if exit_code == 1 else "success",
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "validate",
        "command": "python benchmark_scripts/summarize_results.py",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
            "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        },
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo),
            "timestamp_utc": _utc_timestamp(),
            "env_vars": {
                k: ("***REDACTED***" if k in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "OPENAI_API_KEY"} else v)
                for k, v in os.environ.items()
                if k
                in {
                    "CUDA_VISIBLE_DEVICES",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                    "HF_TOKEN",
                    "HUGGINGFACE_HUB_TOKEN",
                    "OPENAI_API_KEY",
                }
            },
            "decision_reason": "Read per-stage build_output/*/results.json in execution order and emit an overall summary with key metrics.",
        },
        "failure_category": "unknown" if exit_code == 0 else "unknown",
        "error_excerpt": _tail_text(log_path, max_lines=220),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as logf:
        logf.write("\n--- summary ---\n")
        logf.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
