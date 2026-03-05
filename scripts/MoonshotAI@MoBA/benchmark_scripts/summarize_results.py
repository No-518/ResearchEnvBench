#!/usr/bin/env python3
from __future__ import annotations

import datetime as _dt
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def _read_stage_results(results_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not results_path.exists():
        return None, "missing_stage_results"
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception:
        return None, "invalid_json"


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    summary_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).name))}"

    failed_stages: List[str] = []
    skipped_stages: List[str] = []
    stages_out: Dict[str, Any] = {}

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    log_lines: List[str] = []
    overall_failure = False
    any_missing_or_invalid = False
    worst_failure_category = "unknown"

    for stage in STAGES:
        stage_dir = repo_root / "build_output" / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"
        data, err = _read_stage_results(stage_results_path)

        if data is None:
            any_missing_or_invalid = True
            worst_failure_category = err or worst_failure_category
            overall_failure = True
            failed_stages.append(stage)
            stages_out[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
            log_lines.append(f"{stage}: {err} ({stage_results_path})")
            continue

        status = str(data.get("status") or "failure")
        try:
            exit_code = int(data.get("exit_code") if data.get("exit_code") is not None else 0)
        except Exception:
            exit_code = 1

        failure_category = str(data.get("failure_category") or "")
        stage_command = str(data.get("command") or "")

        stages_out[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": stage_command,
            "results_path": str(stage_results_path),
            "log_path": str(stage_log_path),
        }

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            overall_failure = True
            failed_stages.append(stage)

        # Metrics aggregation.
        if stage == "pyright":
            m = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
            missing = m.get("missing_packages_count")
            total = m.get("total_imported_packages_count")
            ratio = m.get("missing_package_ratio")
            try:
                missing_i = int(missing)  # type: ignore[arg-type]
                total_i = int(total)  # type: ignore[arg-type]
                ratio_out: Any = f"{missing_i}/{total_i}"
            except Exception:
                ratio_out = ratio
            metrics["pyright"] = {
                "missing_packages_count": missing,
                "total_imported_packages_count": total,
                "missing_package_ratio": ratio_out,
            }
        elif stage == "env_size":
            obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            metrics["env_size"] = {
                "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
            }
        elif stage == "hallucination":
            h = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            def _count(kind: str) -> Any:
                obj = h.get(kind)
                return obj.get("count") if isinstance(obj, dict) else None
            metrics["hallucination"] = {
                "path_hallucinations": _count("path"),
                "version_hallucinations": _count("version"),
                "capability_hallucinations": _count("capability"),
            }

    overall_status = "failure" if overall_failure else "success"
    exit_code = 1 if overall_failure else 0
    failure_category = worst_failure_category if any_missing_or_invalid else ("unknown" if overall_failure else "unknown")
    error_excerpt = ""
    if overall_failure:
        error_excerpt = "failed_stages=" + ",".join(failed_stages)

    summary: Dict[str, Any] = {
        "status": overall_status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "validate",
        "command": summary_command,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_now_iso()},
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _write_json(results_path, summary)
    return 1 if overall_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
