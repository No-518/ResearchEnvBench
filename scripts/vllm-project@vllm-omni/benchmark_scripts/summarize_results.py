#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
from typing import Any


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


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def git_commit(repo: pathlib.Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return ""


def read_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    repo = repo_root()
    out_dir = repo / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages: dict[str, dict[str, Any]] = {}
    failed_stages: list[str] = []
    skipped_stages: list[str] = []

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[summary] time_utc={now_utc_iso()}\n")

        for stage in STAGES_IN_ORDER:
            stage_dir = repo / "build_output" / stage
            stage_results_path = stage_dir / "results.json"
            stage_log_path = stage_dir / "log.txt"

            if not stage_results_path.exists():
                rec = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": "missing_stage_results",
                    "command": "",
                    "results_path": str(stage_results_path),
                    "log_path": str(stage_log_path),
                }
                stages[stage] = rec
                failed_stages.append(stage)
                log_fp.write(f"[summary] {stage}: missing results.json\n")
                continue

            try:
                data = read_json(stage_results_path)
            except Exception as e:
                rec = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": "invalid_json",
                    "command": "",
                    "results_path": str(stage_results_path),
                    "log_path": str(stage_log_path),
                    "error": f"{type(e).__name__}: {e}",
                }
                stages[stage] = rec
                failed_stages.append(stage)
                log_fp.write(f"[summary] {stage}: invalid JSON\n")
                continue

            status = data.get("status") if isinstance(data.get("status"), str) else "failure"
            exit_code = data.get("exit_code")
            if not isinstance(exit_code, int):
                exit_code = 1 if status == "failure" else 0
            failure_category = data.get("failure_category") if isinstance(data.get("failure_category"), str) else ""
            command = data.get("command") if isinstance(data.get("command"), str) else ""

            rec = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
            stages[stage] = rec

            if status == "skipped":
                skipped_stages.append(stage)
            elif status == "failure" or exit_code == 1:
                failed_stages.append(stage)

    overall_status = "failure" if failed_stages else "success"
    status = "success" if overall_status == "success" else "failure"
    exit_code = 0 if overall_status == "success" else 1

    # Aggregated metrics (best-effort).
    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}
    py = stages.get("pyright")
    if py:
        try:
            py_data = read_json(pathlib.Path(py["results_path"]))
            metrics["pyright"] = {
                "missing_packages_count": py_data.get("missing_packages_count"),
                "total_imported_packages_count": py_data.get("total_imported_packages_count"),
                "missing_package_ratio": py_data.get("missing_package_ratio"),
            }
        except Exception:
            pass

    env = stages.get("env_size")
    if env:
        try:
            env_data = read_json(pathlib.Path(env["results_path"]))
            obs = env_data.get("observed") if isinstance(env_data.get("observed"), dict) else {}
            metrics["env_size"] = {
                "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
            }
        except Exception:
            pass

    hall = stages.get("hallucination")
    if hall:
        try:
            h_data = read_json(pathlib.Path(hall["results_path"]))
            h = h_data.get("hallucinations") if isinstance(h_data.get("hallucinations"), dict) else {}
            metrics["hallucination"] = {
                "path": (h.get("path") or {}).get("count", 0) if isinstance(h.get("path"), dict) else 0,
                "version": (h.get("version") or {}).get("count", 0) if isinstance(h.get("version"), dict) else 0,
                "capability": (h.get("capability") or {}).get("count", 0) if isinstance(h.get("capability"), dict) else 0,
            }
        except Exception:
            pass

    result = {
        "stage": "summary",
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "summarize",
        "command": "summarize_results.py",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages,
        "metrics": metrics,
        "failure_category": "" if overall_status == "success" else "runtime",
        "meta": {"git_commit": git_commit(repo), "timestamp_utc": now_utc_iso()},
    }

    write_json(results_path, result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
