#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _report_path(cli_report_path: str | None) -> Path:
    if cli_report_path and cli_report_path.strip():
        return Path(cli_report_path.strip())
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report and env_report.strip():
        return Path(env_report.strip())
    return Path("/opt/scimlopsbench/report.json")


def _tail(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:]).strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--report-path", default=None)
    args = p.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _report_path(args.report_path)

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[cuda] utc_start={_utc_timestamp()}\n")
        log_fp.write(f"[cuda] report_path={report_path}\n")

        status = "failure"
        failure_category = "unknown"
        observed: dict[str, Any] = {"cuda_available": False, "gpu_count": 0, "framework": "unknown"}
        command = ""

        python_path = ""
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            python_path = str(report.get("python_path", "")).strip()
        except Exception as e:
            failure_category = "missing_report"
            log_fp.write(f"[cuda] error: failed to read/parse report: {e}\n")
        else:
            if not python_path:
                failure_category = "missing_report"
                log_fp.write("[cuda] error: report missing python_path\n")
            else:
                snippet = r"""
import json, sys
out = {}
try:
    import torch
    out["framework"] = "pytorch"
    out["torch_version"] = getattr(torch, "__version__", "")
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count()) if out["cuda_available"] else 0
except Exception as e_torch:
    try:
        import tensorflow as tf
        out["framework"] = "tensorflow"
        gpus = tf.config.list_physical_devices("GPU")
        out["cuda_available"] = bool(gpus)
        out["gpu_count"] = len(gpus)
        out["tensorflow_version"] = getattr(tf, "__version__", "")
    except Exception as e_tf:
        try:
            import jax
            out["framework"] = "jax"
            devs = jax.devices()
            gpu = [d for d in devs if getattr(d, "platform", "") == "gpu"]
            out["cuda_available"] = bool(gpu)
            out["gpu_count"] = len(gpu)
            out["jax_version"] = getattr(jax, "__version__", "")
        except Exception as e_jax:
            out["framework"] = "unknown"
            out["cuda_available"] = False
            out["gpu_count"] = 0
            out["error"] = f"torch={e_torch!r}; tf={e_tf!r}; jax={e_jax!r}"
print(json.dumps(out))
"""
                command = f"{python_path} -c <cuda_check_snippet>"
                try:
                    cp = subprocess.run(
                        [python_path, "-c", snippet],
                        cwd=repo_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    log_fp.write(cp.stdout)
                    if cp.stderr:
                        log_fp.write(cp.stderr)
                    if cp.returncode != 0:
                        failure_category = "runtime"
                    else:
                        try:
                            observed = json.loads(cp.stdout.strip().splitlines()[-1])
                        except Exception as e:
                            failure_category = "invalid_json"
                            log_fp.write(f"[cuda] error: failed to parse snippet output: {e}\n")
                        else:
                            if observed.get("cuda_available") is True:
                                status = "success"
                                failure_category = "not_applicable"
                            else:
                                status = "failure"
                                failure_category = "insufficient_hardware"
                except FileNotFoundError:
                    failure_category = "path_hallucination"
                    log_fp.write(f"[cuda] error: python_path not found: {python_path}\n")
                except Exception as e:
                    failure_category = "runtime"
                    log_fp.write(f"[cuda] error: exception running snippet: {e}\n")

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    manifest = repo_root / "benchmark_assets" / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            a = m.get("assets", m)
            assets = {
                "dataset": a.get("dataset", assets["dataset"]),
                "model": a.get("model", assets["model"]),
            }
        except Exception:
            pass

    payload: dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": 0 if status == "success" else 1,
        "stage": "cuda",
        "task": "check",
        "command": command or f"python {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": observed.get("framework", "unknown"),
        "assets": assets,
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "timestamp_utc": _utc_timestamp(),
            "git_commit": _git_commit(repo_root),
            "env_vars": {k: os.environ.get(k, "") for k in sorted(os.environ) if k.startswith("SCIMLOPSBENCH_")},
            "report_path": str(report_path),
            "reported_python_path": python_path,
        },
        "failure_category": failure_category,
        "error_excerpt": "" if status == "success" else _tail(log_path),
    }

    _write_json(results_path, payload)
    return payload["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
