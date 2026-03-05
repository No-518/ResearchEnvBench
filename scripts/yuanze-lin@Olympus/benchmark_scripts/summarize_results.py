#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=5)
            .strip()
        )
    except Exception:
        return ""


def safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_assets(root: Path) -> Dict[str, Dict[str, str]]:
    manifest = root / "benchmark_assets" / "manifest.json"
    data, _err = safe_read_json(manifest)
    if not isinstance(data, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    ds = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
    md = data.get("model") if isinstance(data.get("model"), dict) else {}
    return {
        "dataset": {
            "path": str(ds.get("path", "")),
            "source": str(ds.get("source", "")),
            "version": str(ds.get("version", "")),
            "sha256": str(ds.get("sha256", "")),
        },
        "model": {
            "path": str(md.get("path", "")),
            "source": str(md.get("source", "")),
            "version": str(md.get("version", "")),
            "sha256": str(md.get("sha256", "")),
        },
    }


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_path.write_text("", encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[summary] timestamp_utc={utc_ts()}\n")

        assets = load_assets(root)

        stage_entries: Dict[str, Dict[str, Any]] = {}
        failed_stages: List[str] = []
        skipped_stages: List[str] = []

        for stage in STAGES:
            stage_dir = root / "build_output" / stage
            rpath = stage_dir / "results.json"
            lpath = stage_dir / "log.txt"
            data, err = safe_read_json(rpath)

            if data is None or not isinstance(data, dict):
                entry = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": err,
                    "command": "",
                    "results_path": str(rpath),
                    "log_path": str(lpath),
                }
                stage_entries[stage] = entry
                failed_stages.append(stage)
                log.write(f"[summary] {stage}: {err}\n")
                continue

            status = str(data.get("status", "failure"))
            try:
                exit_code = int(data.get("exit_code", 1))
            except Exception:
                exit_code = 1
            failure_category = str(data.get("failure_category", "unknown"))
            command = str(data.get("command", ""))

            entry = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(rpath),
                "log_path": str(lpath),
            }
            stage_entries[stage] = entry

            if status == "skipped":
                skipped_stages.append(stage)
            elif status == "failure" or exit_code == 1:
                failed_stages.append(stage)

        overall_status = "success"
        if any(stage_entries[s]["status"] == "failure" or stage_entries[s]["exit_code"] == 1 for s in STAGES):
            overall_status = "failure"

        # Aggregated metrics (best-effort).
        metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

        pyright_data, _ = safe_read_json(root / "build_output" / "pyright" / "results.json")
        if isinstance(pyright_data, dict):
            m = pyright_data.get("metrics")
            if isinstance(m, dict):
                for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
                    if k in m:
                        metrics["pyright"][k] = m.get(k)

        env_data, _ = safe_read_json(root / "build_output" / "env_size" / "results.json")
        if isinstance(env_data, dict):
            obs = env_data.get("observed")
            if isinstance(obs, dict):
                for k in ["env_prefix_size_MB", "site_packages_total_bytes"]:
                    if k in obs:
                        metrics["env_size"][k] = obs.get(k)

        hall_data, _ = safe_read_json(root / "build_output" / "hallucination" / "results.json")
        if isinstance(hall_data, dict):
            h = hall_data.get("hallucinations")
            if isinstance(h, dict):
                for kind in ["path", "version", "capability"]:
                    node = h.get(kind)
                    if isinstance(node, dict) and "count" in node:
                        metrics["hallucination"][f"{kind}_count"] = node.get("count")

        status = "success" if overall_status == "success" else "failure"
        exit_code = 0 if status == "success" else 1

        summary_payload = {
            "status": status,
            "skip_reason": "not_applicable",
            "exit_code": exit_code,
            "stage": "summary",
            "task": "summarize",
            "command": f"{sys.executable} benchmark_scripts/summarize_results.py",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {"dataset": assets["dataset"], "model": assets["model"]},
            "overall_status": overall_status,
            "failed_stages": failed_stages,
            "skipped_stages": skipped_stages,
            "stages": stage_entries,
            "metrics": metrics,
            "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_ts()},
            "failure_category": "unknown" if status == "success" else "unknown",
            "error_excerpt": "",
        }

        write_json(results_path, summary_payload)
        log.write(f"[summary] overall_status={overall_status}\n")

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
