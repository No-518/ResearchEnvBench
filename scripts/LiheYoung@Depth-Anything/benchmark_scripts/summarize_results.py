#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
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


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def git_commit(root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        return ""
    return ""


def safe_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def stage_paths(root: Path, stage: str) -> Tuple[Path, Path]:
    d = root / "build_output" / stage
    return d / "results.json", d / "log.txt"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    ts = datetime.now(tz=timezone.utc).isoformat()

    stages_summary: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write("== summarize_results ==\n")
        log_f.write(f"timestamp_utc: {ts}\n")

        for stage in STAGES_IN_ORDER:
            res_path, lg_path = stage_paths(root, stage)
            data, err = load_json(res_path)
            if data is None:
                status = "failure"
                exit_code = 1
                failure_category = err or "invalid_json"
                command = ""
                stages_summary[stage] = {
                    "status": status,
                    "exit_code": exit_code,
                    "failure_category": failure_category,
                    "command": command,
                    "results_path": str(res_path),
                    "log_path": str(lg_path),
                }
                failed.append(stage)
                log_f.write(f"{stage}: {failure_category}\n")
                continue

            status = data.get("status", "failure")
            exit_code = int(data.get("exit_code", 1)) if str(data.get("exit_code", "")).isdigit() else data.get("exit_code", 1)
            failure_category = data.get("failure_category", "unknown")
            command = data.get("command", "")

            stages_summary[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(res_path),
                "log_path": str(lg_path),
            }

            if status == "skipped":
                skipped.append(stage)
            elif status == "failure" or int(exit_code) == 1:
                failed.append(stage)

            # Aggregate metrics when present.
            if stage == "pyright":
                meta = data.get("meta") or {}
                m = {}
                if isinstance(meta, dict):
                    m = meta.get("metrics") or {}
                if isinstance(m, dict):
                    for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
                        if k in m:
                            metrics["pyright"][k] = m.get(k)
            elif stage == "env_size":
                obs = data.get("observed") or {}
                if isinstance(obs, dict):
                    if "env_prefix_size_MB" in obs:
                        metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                    if "site_packages_total_bytes" in obs:
                        metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
            elif stage == "hallucination":
                h = data.get("hallucinations") or {}
                if isinstance(h, dict):
                    for kind in ["path", "version", "capability"]:
                        sub = h.get(kind) or {}
                        if isinstance(sub, dict) and "count" in sub:
                            metrics["hallucination"][f"{kind}_count"] = sub.get("count")

    overall_status = "failure" if failed else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    summary_payload: Dict[str, Any] = {
        "stage": "summary",
        "status": status,
        "exit_code": exit_code,
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(root), "timestamp_utc": ts},
    }

    safe_write_json(results_path, summary_payload)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        root = repo_root()
        out_dir = root / "build_output" / "summary"
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "log.txt"
        results_path = out_dir / "results.json"
        with log_path.open("a", encoding="utf-8") as f:
            f.write("fatal exception\n")
            f.write(traceback.format_exc() + "\n")
        safe_write_json(
            results_path,
            {
                "stage": "summary",
                "status": "failure",
                "exit_code": 1,
                "overall_status": "failure",
                "failed_stages": STAGES_IN_ORDER,
                "skipped_stages": [],
                "stages": {},
                "metrics": {},
                "meta": {"timestamp_utc": datetime.now(tz=timezone.utc).isoformat()},
                "failure_category": "unknown",
                "error_excerpt": "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]),
            },
        )
        raise SystemExit(1)
