#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "invalid_json"


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    logs: List[str] = []
    logs.append(f"[summary] timestamp_utc={_utc_timestamp()}")

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES_IN_ORDER:
        stage_dir = repo_root / "build_output" / stage
        res_path = stage_dir / "results.json"
        lg_path = stage_dir / "log.txt"
        data, err_cat = _safe_json_load(res_path)

        if data is None:
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err_cat,
                "command": "",
                "results_path": str(res_path),
                "log_path": str(lg_path),
            }
            failed.append(stage)
            logs.append(f"[summary] {stage}: failure ({err_cat})")
            continue

        status = str(data.get("status", "failure"))
        exit_code = int(data.get("exit_code", 1))
        failure_category = str(data.get("failure_category", "unknown"))
        command = str(data.get("command", ""))

        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(res_path),
            "log_path": str(lg_path),
        }

        if status == "skipped":
            skipped.append(stage)
            logs.append(f"[summary] {stage}: skipped")
        elif status == "failure" or exit_code == 1:
            failed.append(stage)
            logs.append(f"[summary] {stage}: failure ({failure_category})")
        else:
            logs.append(f"[summary] {stage}: success")

        if stage == "pyright":
            for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
                if k in data:
                    metrics["pyright"][k] = data.get(k)
        if stage == "env_size":
            observed = data.get("observed", {}) if isinstance(data.get("observed"), dict) else {}
            if "env_prefix_size_MB" in observed:
                metrics["env_size"]["env_prefix_size_MB"] = observed.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in observed:
                metrics["env_size"]["site_packages_total_bytes"] = observed.get("site_packages_total_bytes")
        if stage == "hallucination":
            halluc = data.get("hallucinations", {}) if isinstance(data.get("hallucinations"), dict) else {}
            for key in ["path", "version", "capability"]:
                if isinstance(halluc.get(key), dict) and "count" in halluc[key]:
                    metrics["hallucination"][f"{key}_count"] = halluc[key]["count"]

    overall_status = "failure" if any(stages[s]["status"] == "failure" or stages[s]["exit_code"] == 1 for s in stages) else "success"
    exit_code = 1 if overall_status == "failure" else 0

    payload: Dict[str, Any] = {
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {"timestamp_utc": _utc_timestamp()},
    }

    log_path.write_text("\n".join(logs) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

