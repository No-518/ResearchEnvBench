#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_json_load(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
        return out or None
    except Exception:
        return None


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_out: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    for stage in STAGES_ORDER:
        stage_dir = repo_root / "build_output" / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"

        obj, err_kind = _safe_json_load(stage_results_path)
        if obj is None:
            stage_status = "failure"
            stage_exit_code = 1
            stage_failure_category = err_kind
            stage_command = ""
        else:
            stage_status = obj.get("status", "failure")
            try:
                stage_exit_code = int(obj.get("exit_code", 1))
            except Exception:
                stage_exit_code = 1
            stage_failure_category = obj.get("failure_category", "unknown")
            stage_command = obj.get("command", "")

        stage_entry = {
            "status": stage_status,
            "exit_code": stage_exit_code,
            "failure_category": stage_failure_category,
            "command": stage_command,
            "results_path": str(stage_results_path),
            "log_path": str(stage_log_path),
        }
        stages_out[stage] = stage_entry

        if stage_status == "skipped":
            skipped_stages.append(stage)
        elif stage_status == "failure" or stage_exit_code == 1:
            failed_stages.append(stage)

    overall_status = "failure" if failed_stages else "success"

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    pyright_obj, _ = _safe_json_load(repo_root / "build_output" / "pyright" / "results.json")
    if isinstance(pyright_obj, dict):
        m = pyright_obj.get("metrics") if isinstance(pyright_obj.get("metrics"), dict) else {}
        if isinstance(m, dict):
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in m:
                    metrics["pyright"][k] = m.get(k)

    env_obj, _ = _safe_json_load(repo_root / "build_output" / "env_size" / "results.json")
    if isinstance(env_obj, dict):
        obs = env_obj.get("observed") if isinstance(env_obj.get("observed"), dict) else {}
        if isinstance(obs, dict):
            if "env_prefix_size_MB" in obs:
                metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
            if "site_packages_total_bytes" in obs:
                metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")

    hall_obj, _ = _safe_json_load(repo_root / "build_output" / "hallucination" / "results.json")
    if isinstance(hall_obj, dict):
        h = hall_obj.get("hallucinations") if isinstance(hall_obj.get("hallucinations"), dict) else {}
        if isinstance(h, dict):
            for k in ("path", "version", "capability"):
                if isinstance(h.get(k), dict) and "count" in h[k]:
                    metrics["hallucination"][f"{k}_count"] = h[k].get("count")

    summary = {
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_now_iso()},
    }

    log_lines = [
        f"[summary] timestamp_utc={summary['meta']['timestamp_utc']}",
        f"[summary] overall_status={overall_status}",
        f"[summary] failed_stages={failed_stages}",
        f"[summary] skipped_stages={skipped_stages}",
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _write_json(results_path, summary)

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
