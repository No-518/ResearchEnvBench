#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGES_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {e}"
    except Exception as e:
        return None, f"read_error: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize benchmark stage results.")
    parser.add_argument("--out-root", type=str, default="build_output")
    args = parser.parse_args()

    out_root = (REPO_ROOT / args.out_root).resolve()
    summary_dir = out_root / "summary"
    ensure_dir(summary_dir)
    log_path = summary_dir / "log.txt"
    results_path = summary_dir / "results.json"

    stages_summary: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    log_lines: List[str] = [f"[{utc_now_iso()}] out_root={out_root}"]

    for stage in STAGES_ORDER:
        stage_dir = out_root / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"

        data, err = read_json(stage_results_path)
        if data is None:
            failure_category = "missing_stage_results" if err == "missing" else "invalid_json"
            status = "failure"
            exit_code = 1
            command = ""
            stages_summary[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
            failed_stages.append(stage)
            log_lines.append(f"[{utc_now_iso()}] {stage}: failure ({failure_category})")
            continue

        if not isinstance(data, dict):
            stages_summary[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "invalid_json",
                "command": "",
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
            failed_stages.append(stage)
            log_lines.append(f"[{utc_now_iso()}] {stage}: failure (invalid_json)")
            continue

        status = str(data.get("status") or "failure")
        try:
            exit_code = int(data.get("exit_code") or 0)
        except Exception:
            exit_code = 1
        failure_category = str(data.get("failure_category") or "")
        command = str(data.get("command") or "")

        stages_summary[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(stage_results_path),
            "log_path": str(stage_log_path),
        }

        if status == "skipped":
            skipped_stages.append(stage)
            log_lines.append(f"[{utc_now_iso()}] {stage}: skipped")
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)
            log_lines.append(f"[{utc_now_iso()}] {stage}: failure ({failure_category or 'unknown'})")
        else:
            log_lines.append(f"[{utc_now_iso()}] {stage}: success")

    overall_status = "failure" if failed_stages else "success"

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    # Pyright metrics
    pyright_results, _ = read_json(out_root / "pyright" / "results.json")
    if isinstance(pyright_results, dict):
        m = pyright_results.get("metrics") if isinstance(pyright_results.get("metrics"), dict) else pyright_results
        metrics["pyright"] = {
            "missing_packages_count": m.get("missing_packages_count"),
            "total_imported_packages_count": m.get("total_imported_packages_count"),
            "missing_package_ratio": m.get("missing_package_ratio"),
        }

    # Env size metrics
    env_results, _ = read_json(out_root / "env_size" / "results.json")
    if isinstance(env_results, dict):
        obs = env_results.get("observed") if isinstance(env_results.get("observed"), dict) else {}
        metrics["env_size"] = {
            "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
            "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
        }

    # Hallucination metrics
    hall_results, _ = read_json(out_root / "hallucination" / "results.json")
    if isinstance(hall_results, dict):
        h = hall_results.get("hallucinations") if isinstance(hall_results.get("hallucinations"), dict) else {}
        metrics["hallucination"] = {
            "path_hallucinations": (h.get("path") or {}).get("count") if isinstance(h.get("path"), dict) else None,
            "version_hallucinations": (h.get("version") or {}).get("count") if isinstance(h.get("version"), dict) else None,
            "capability_hallucinations": (h.get("capability") or {}).get("count") if isinstance(h.get("capability"), dict) else None,
        }

    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    summary_payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": f"python {Path(__file__).name} --out-root {args.out_root}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "failure_category": "" if exit_code == 0 else "overall_failure",
        "error_excerpt": "",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(), "timestamp_utc": utc_now_iso()},
    }

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    if exit_code != 0:
        summary_payload["error_excerpt"] = "\n".join(log_lines[-50:])
    results_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
