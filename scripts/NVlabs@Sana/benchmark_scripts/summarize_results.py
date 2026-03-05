#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
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


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def git_commit(root: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = time.time()

    stages: Dict[str, Dict[str, Any]] = {}
    failed: List[str] = []
    skipped: List[str] = []
    status = "success"
    exit_code = 0
    failure_category = ""

    with log_path.open("w", encoding="utf-8") as log:
        for stage in STAGES_IN_ORDER:
            p = root / "build_output" / stage / "results.json"
            entry: Dict[str, Any] = {"results_path": str(p), "log_path": str(p.parent / "log.txt")}
            if not p.exists():
                entry.update({"status": "failure", "exit_code": 1, "failure_category": "missing_stage_results", "command": ""})
                stages[stage] = entry
                failed.append(stage)
                log.write(f"[summary] missing results for {stage}: {p}\n")
                continue
            try:
                data = read_json(p)
                if not isinstance(data, dict):
                    raise ValueError("stage results not an object")
                entry.update(
                    {
                        "status": data.get("status", "failure"),
                        "exit_code": int(data.get("exit_code", 1)),
                        "failure_category": data.get("failure_category", ""),
                        "command": data.get("command", ""),
                    }
                )
                stages[stage] = entry
                if entry["status"] == "skipped":
                    skipped.append(stage)
                elif entry["status"] == "failure" or entry["exit_code"] == 1:
                    failed.append(stage)
            except Exception as e:
                entry.update({"status": "failure", "exit_code": 1, "failure_category": "invalid_json", "command": ""})
                stages[stage] = entry
                failed.append(stage)
                log.write(f"[summary] invalid json for {stage}: {p}: {e}\n")

        overall_status = "failure" if failed else "success"

        # Aggregated metrics (best-effort).
        metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}
        try:
            pr = read_json(root / "build_output" / "pyright" / "results.json")
            metrics["pyright"] = {
                "missing_packages_count": pr.get("missing_packages_count", None),
                "total_imported_packages_count": pr.get("total_imported_packages_count", None),
                "missing_package_ratio": pr.get("missing_package_ratio", None),
            }
        except Exception:
            pass
        try:
            es = read_json(root / "build_output" / "env_size" / "results.json")
            obs = es.get("observed", {}) if isinstance(es, dict) else {}
            metrics["env_size"] = {
                "env_prefix_size_MB": obs.get("env_prefix_size_MB", None),
                "site_packages_total_bytes": obs.get("site_packages_total_bytes", None),
            }
        except Exception:
            pass
        try:
            hr = read_json(root / "build_output" / "hallucination" / "results.json")
            hall = hr.get("hallucinations", {}) if isinstance(hr, dict) else {}
            metrics["hallucination"] = {
                "path_hallucinations": hall.get("path", {}).get("count", None),
                "version_hallucinations": hall.get("version", {}).get("count", None),
                "capability_hallucinations": hall.get("capability", {}).get("count", None),
            }
        except Exception:
            pass

        payload = {
            # Stage envelope (run_all.sh reads these fields)
            "status": status,
            "skip_reason": "not_applicable",
            "exit_code": exit_code,
            "failure_category": failure_category,
            "stage": "summary",
            "task": "aggregate",
            "command": "benchmark_scripts/summarize_results.py",
            "overall_status": overall_status,
            "failed_stages": failed,
            "skipped_stages": skipped,
            "stages": stages,
            "metrics": metrics,
            "meta": {
                "git_commit": git_commit(root),
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_sec": round(time.time() - started, 3),
            },
        }

        tmp = results_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(results_path)

        log.write(f"[summary] overall_status={overall_status}\n")
        log.write(f"[summary] failed_stages={failed}\n")
        log.write(f"[summary] skipped_stages={skipped}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
