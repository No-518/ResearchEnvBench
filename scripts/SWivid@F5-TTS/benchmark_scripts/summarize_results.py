#!/usr/bin/env python3
import datetime as dt
import json
import subprocess
import sys
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
    return Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_git_commit(root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return (cp.stdout or "").strip()
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception as e:
        return None, f"read_error:{type(e).__name__}:{e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception:
        return None, "invalid_json"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_stage_record(stage: str, data: Optional[dict], err: Optional[str], root: Path) -> Dict[str, Any]:
    results_path = root / "build_output" / stage / "results.json"
    log_path = root / "build_output" / stage / "log.txt"

    if err is not None or not isinstance(data, dict):
        failure_category = "invalid_json" if err == "invalid_json" else "missing_stage_results"
        return {
            "stage": stage,
            "status": "failure",
            "exit_code": 1,
            "failure_category": failure_category,
            "command": "",
            "results_path": str(results_path),
            "log_path": str(log_path),
        }

    status = data.get("status")
    if not isinstance(status, str):
        status = "failure"

    try:
        exit_code = int(data.get("exit_code", 1))
    except Exception:
        exit_code = 1

    failure_category = data.get("failure_category")
    if not isinstance(failure_category, str):
        failure_category = "unknown"

    command = data.get("command")
    if not isinstance(command, str):
        command = ""

    return {
        "stage": stage,
        "status": status,
        "exit_code": exit_code,
        "failure_category": failure_category,
        "command": command,
        "results_path": str(results_path),
        "log_path": str(log_path),
    }


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    command_str = f"{sys.executable} {Path(__file__).as_posix()}"

    stages: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    pyright_metrics: Dict[str, Any] = {}
    env_size_metrics: Dict[str, Any] = {}
    hallucination_metrics: Dict[str, Any] = {}

    for stage in STAGES_IN_ORDER:
        data, err = read_json(root / "build_output" / stage / "results.json")
        record = normalize_stage_record(stage, data, err, root)
        stages[stage] = record

        status = record["status"]
        exit_code = record["exit_code"]

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

        if stage == "pyright" and isinstance(data, dict):
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in data:
                    pyright_metrics[k] = data.get(k)
        if stage == "env_size" and isinstance(data, dict):
            observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            if isinstance(observed, dict):
                if isinstance(observed.get("env_prefix_size_MB"), int):
                    env_size_metrics["env_prefix_size_MB"] = observed["env_prefix_size_MB"]
                if isinstance(observed.get("site_packages_total_bytes"), int):
                    env_size_metrics["site_packages_total_bytes"] = observed["site_packages_total_bytes"]
        if stage == "hallucination" and isinstance(data, dict):
            hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            if isinstance(hall, dict):
                for k, out_key in (
                    ("path", "path_hallucinations"),
                    ("version", "version_hallucinations"),
                    ("capability", "capability_hallucinations"),
                ):
                    node = hall.get(k) if isinstance(hall.get(k), dict) else {}
                    if isinstance(node, dict) and isinstance(node.get("count"), int):
                        hallucination_metrics[out_key] = node["count"]

    overall_status = "failure" if failed_stages else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if status == "failure" else 0

    payload: Dict[str, Any] = {
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
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages,
        "metrics": {
            "pyright": pyright_metrics,
            "env_size": env_size_metrics,
            "hallucination": hallucination_metrics,
        },
        "meta": {
            "git_commit": safe_git_commit(root),
            "timestamp_utc": utc_now(),
        },
        "failure_category": "unknown" if status == "success" else "runtime",
        "error_excerpt": "" if status == "success" else "One or more stages failed",
    }

    log_lines = [f"[summary] overall_status={overall_status} exit_code={exit_code}"]
    for st in STAGES_IN_ORDER:
        rec = stages.get(st, {})
        log_lines.append(f"[summary] {st}: status={rec.get('status')} exit_code={rec.get('exit_code')}")
    if failed_stages:
        log_lines.append(f"[summary] failed_stages={failed_stages}")
    if skipped_stages:
        log_lines.append(f"[summary] skipped_stages={skipped_stages}")

    write_text(log_path, "\n".join(log_lines) + "\n")
    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
