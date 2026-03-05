#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple


STAGES_ORDER = [
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
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
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

    stages_out: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}
    self_command = " ".join(
        shlex.quote(x)
        for x in [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    )

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[summary] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[summary] command={self_command}\n")

        for stage in STAGES_ORDER:
            res_path = root / "build_output" / stage / "results.json"
            lg_path = root / "build_output" / stage / "log.txt"
            data, err = read_json(res_path)
            if err == "missing":
                stage_entry = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": "missing_stage_results",
                    "command": "",
                    "results_path": str(res_path),
                    "log_path": str(lg_path),
                }
                failed.append(stage)
                stages_out[stage] = stage_entry
                logf.write(f"[summary] {stage}: missing results.json\n")
                continue
            if err == "invalid_json":
                stage_entry = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": "invalid_json",
                    "command": "",
                    "results_path": str(res_path),
                    "log_path": str(lg_path),
                }
                failed.append(stage)
                stages_out[stage] = stage_entry
                logf.write(f"[summary] {stage}: invalid results.json\n")
                continue

            assert data is not None
            status = str(data.get("status", "failure"))
            exit_code_raw = data.get("exit_code", 1)
            try:
                exit_code = int(exit_code_raw)  # type: ignore[arg-type]
            except Exception:
                exit_code = 1
            failure_category = str(data.get("failure_category", "unknown"))
            stage_command = str(data.get("command", ""))

            stage_entry = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": stage_command,
                "results_path": str(res_path),
                "log_path": str(lg_path),
            }
            stages_out[stage] = stage_entry

            if status == "skipped":
                skipped.append(stage)
            elif status == "failure" or exit_code == 1:
                failed.append(stage)

            # Aggregate metrics if present.
            if stage == "pyright":
                m = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
                for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
                    if k in m:
                        metrics["pyright"][k] = m.get(k)
            elif stage == "env_size":
                obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
                if "env_prefix_size_MB" in obs:
                    metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                if "site_packages_total_bytes" in obs:
                    metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
            elif stage == "hallucination":
                h = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
                for kind in ["path", "version", "capability"]:
                    sub = h.get(kind) if isinstance(h.get(kind), dict) else {}
                    if "count" in sub:
                        metrics["hallucination"][f"{kind}_count"] = sub.get("count")

        overall_status = "failure" if failed else "success"
        logf.write(f"[summary] failed_stages={failed}\n")
        logf.write(f"[summary] skipped_stages={skipped}\n")
        logf.write(f"[summary] overall_status={overall_status}\n")

    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    summary_failure_category = "unknown"
    if overall_status == "failure":
        if any(stages_out.get(s, {}).get("failure_category") == "missing_stage_results" for s in STAGES_ORDER):
            summary_failure_category = "missing_stage_results"
        elif any(stages_out.get(s, {}).get("failure_category") == "invalid_json" for s in STAGES_ORDER):
            summary_failure_category = "invalid_json"

    summary = {
        "stage": "summary",
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "summarize",
        "command": self_command,
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
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": {
                k: os.environ.get(k, "")
                for k in [
                    "CUDA_VISIBLE_DEVICES",
                    "HF_HOME",
                    "TRANSFORMERS_CACHE",
                    "HF_DATASETS_CACHE",
                    "PIP_CACHE_DIR",
                    "XDG_CACHE_HOME",
                    "SENTENCE_TRANSFORMERS_HOME",
                    "TORCH_HOME",
                    "PYTHONDONTWRITEBYTECODE",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                ]
            },
            "decision_reason": "Summarize per-stage results.json in fixed execution order; overall_status fails if any stage failed (skipped does not count).",
            "timestamp_utc": utc_timestamp(),
        },
        "failure_category": summary_failure_category,
        "error_excerpt": tail_text(log_path),
    }

    results_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
