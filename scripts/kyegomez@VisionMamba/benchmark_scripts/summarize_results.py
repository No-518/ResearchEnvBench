#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.DEVNULL)
            .strip()
        )
    except Exception:
        return ""


def read_json_file(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    root = repo_root()
    stage_dir = root / "build_output" / "summary"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    stages_order = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]

    stages_out: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("w", encoding="utf-8") as log_f:
        def log(msg: str) -> None:
            log_f.write(msg.rstrip() + "\n")
            log_f.flush()

        for stage in stages_order:
            res_path = root / "build_output" / stage / "results.json"
            stage_log = root / "build_output" / stage / "log.txt"
            data, err = read_json_file(res_path)

            if data is None:
                entry = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": err,
                    "command": "",
                    "results_path": str(res_path),
                    "log_path": str(stage_log),
                }
                stages_out[stage] = entry
                failed.append(stage)
                log(f"[summary] {stage}: {err}")
                continue

            status = str(data.get("status") or "failure")
            exit_code = int(data.get("exit_code", 1))
            failure_category = str(data.get("failure_category") or "unknown")
            command = str(data.get("command") or "")

            stages_out[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(res_path),
                "log_path": str(stage_log),
            }

            if status == "skipped":
                skipped.append(stage)
            elif status == "failure" or exit_code == 1:
                failed.append(stage)

            # Metrics aggregation (best-effort).
            if stage == "pyright":
                for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                    if k in data:
                        metrics["pyright"][k] = data.get(k)
            elif stage == "env_size":
                observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
                if "env_prefix_size_MB" in observed:
                    metrics["env_size"]["env_prefix_size_MB"] = observed.get("env_prefix_size_MB")
                if "site_packages_total_bytes" in observed:
                    metrics["env_size"]["site_packages_total_bytes"] = observed.get("site_packages_total_bytes")
            elif stage == "hallucination":
                hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
                for kind in ("path", "version", "capability"):
                    node = hall.get(kind) if isinstance(hall.get(kind), dict) else {}
                    if "count" in node:
                        metrics["hallucination"][f"{kind}_count"] = node.get("count")

        overall_status = "failure" if failed else "success"
        log(f"[summary] overall_status={overall_status}")
        log(f"[summary] failed_stages={failed}")
        log(f"[summary] skipped_stages={skipped}")

    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if status == "failure" else 0

    summary = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"python {Path(__file__).name}",
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
            "timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }
    write_json(results_path, summary)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
