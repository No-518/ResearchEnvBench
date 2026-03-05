#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import shlex
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception as e:
        return None, f"read_error: {type(e).__name__}: {e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception:
        return None, "invalid_json"


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    _ensure_dir(out_dir)

    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_out: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {
        "pyright": {},
        "env_size": {},
        "hallucination": {},
    }

    for stage in STAGES_IN_ORDER:
        stage_dir = repo_root / "build_output" / stage
        rpath = stage_dir / "results.json"
        lpath = stage_dir / "log.txt"
        data, err = _read_json(rpath)
        if data is None:
            stage_entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_stage_results" if err == "missing_stage_results" else "invalid_json",
                "command": "",
                "results_path": str(rpath),
                "log_path": str(lpath),
            }
            stages_out[stage] = stage_entry
            failed.append(stage)
            continue

        status = str(data.get("status") or "failure")
        exit_code = int(data.get("exit_code") or 0)
        failure_category = str(data.get("failure_category") or "unknown")
        command = str(data.get("command") or "")

        stage_entry = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(rpath),
            "log_path": str(lpath),
        }
        stages_out[stage] = stage_entry

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        if stage == "pyright":
            m = data.get("metrics")
            if isinstance(m, dict):
                metrics["pyright"] = {
                    "missing_packages_count": m.get("missing_packages_count"),
                    "total_imported_packages_count": m.get("total_imported_packages_count"),
                    "missing_package_ratio": m.get("missing_package_ratio"),
                }
        elif stage == "env_size":
            obs = data.get("observed")
            if isinstance(obs, dict):
                metrics["env_size"] = {
                    "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                    "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
                }
        elif stage == "hallucination":
            h = data.get("hallucinations")
            if isinstance(h, dict):
                metrics["hallucination"] = {
                    "path_hallucinations": (h.get("path") or {}).get("count") if isinstance(h.get("path"), dict) else None,
                    "version_hallucinations": (h.get("version") or {}).get("count") if isinstance(h.get("version"), dict) else None,
                    "capability_hallucinations": (h.get("capability") or {}).get("count") if isinstance(h.get("capability"), dict) else None,
                }

    overall_status = "failure" if len(failed) > 0 else "success"
    exit_code = 1 if overall_status == "failure" else 0
    status = overall_status

    command_str = " ".join(shlex.quote(x) for x in [sys.executable, *sys.argv])

    log_text = ""
    log_text += f"overall_status={overall_status}\n"
    log_text += f"failed_stages={failed}\n"
    log_text += f"skipped_stages={skipped}\n"

    summary = {
        "stage": "summary",
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "task": "summarize",
        "command": command_str,
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
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
        },
        "failure_category": "unknown",
        "error_excerpt": log_text.strip(),
    }

    results_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with log_path.open("w", encoding="utf-8") as f:
        f.write(log_text)

    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
