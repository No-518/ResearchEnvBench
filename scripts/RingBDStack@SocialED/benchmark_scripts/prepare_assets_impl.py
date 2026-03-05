#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")
DEFAULT_DATASET = "Event2012"
DEFAULT_DATASET_REPO = "https://github.com/ChenBeici/SocialED_datasets.git"
DEFAULT_MODEL_ID = "sentence-transformers/paraphrase-MiniLM-L6-v2"


REQUIRED_DATASET_COLUMNS = [
    "tweet_id",
    "text",
    "event_id",
    "words",
    "filtered_words",
    "entities",
    "user_id",
    "created_at",
    "urls",
    "hashtags",
    "user_mentions",
]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dir(path: Path) -> str:
    h = hashlib.sha256()
    for file_path in sorted([p for p in path.rglob("*") if p.is_file()]):
        rel = file_path.relative_to(path).as_posix().encode("utf-8")
        h.update(rel + b"\0")
        h.update(sha256_file(file_path).encode("utf-8") + b"\0")
    return h.hexdigest()


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, "missing_report"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def run_cmd(argv: List[str], cwd: Path, env: Dict[str, str]) -> Tuple[int, str]:
    try:
        completed = subprocess.run(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return int(completed.returncode), completed.stdout
    except FileNotFoundError as e:
        return 127, str(e)
    except Exception as e:
        return 1, str(e)


def find_dataset_npy(repo_dir: Path, dataset_name: str) -> Optional[Path]:
    target = f"{dataset_name}.npy"
    for root, _dirs, files in os.walk(repo_dir):
        if target in files:
            return Path(root) / target
    return None


def try_import_huggingface_hub() -> Tuple[bool, Optional[str]]:
    try:
        import huggingface_hub  # noqa: F401

        return True, None
    except Exception as e:
        return False, str(e)


def snapshot_download_model(model_id: str, cache_root: Path, offline_ok: bool) -> Tuple[Optional[Path], str, Optional[str]]:
    ok, err = try_import_huggingface_hub()
    if not ok:
        return None, "", f"huggingface_hub_import_failed: {err}"

    from huggingface_hub import snapshot_download  # type: ignore

    cache_dir = cache_root / "huggingface"
    ensure_dir(cache_dir)

    # Attempt online first, then offline reuse if requested.
    try:
        path = Path(
            snapshot_download(
                repo_id=model_id,
                cache_dir=str(cache_dir),
                local_files_only=False,
            )
        )
        return path, _infer_hf_snapshot_version(path), None
    except Exception as e:
        if not offline_ok:
            return None, "", f"snapshot_download_failed: {e}"
        try:
            path = Path(
                snapshot_download(
                    repo_id=model_id,
                    cache_dir=str(cache_dir),
                    local_files_only=True,
                )
            )
            return path, _infer_hf_snapshot_version(path), None
        except Exception as e2:
            return None, "", f"snapshot_download_offline_failed: {e2}"


def _infer_hf_snapshot_version(model_path: Path) -> str:
    # Prefer returning the snapshot hash if present in the returned path.
    parts = model_path.parts
    if "snapshots" in parts:
        idx = parts.index("snapshots")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def build_results_template(stage: str, task: str, command: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": 1200,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "timestamp_utc": utc_timestamp(),
            "python": sys.executable,
            "git_commit": git_commit(repo_root()),
            "env_vars": {},
            "decision_reason": "",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare benchmark assets (dataset + minimal model).")
    ap.add_argument("--report-path", default=None, help="Override report.json path (default: /opt/scimlopsbench/report.json)")
    ap.add_argument("--dataset", default=os.environ.get("SCIMLOPSBENCH_DATASET", DEFAULT_DATASET))
    ap.add_argument("--dataset-repo", default=os.environ.get("SCIMLOPSBENCH_DATASET_REPO", DEFAULT_DATASET_REPO))
    ap.add_argument("--model-id", default=os.environ.get("SCIMLOPSBENCH_MODEL_ID", DEFAULT_MODEL_ID))
    ap.add_argument("--subset-size", type=int, default=int(os.environ.get("SCIMLOPSBENCH_SUBSET_SIZE", "12")))
    ap.add_argument("--offline-ok", action="store_true", help="Allow offline reuse from cache if download fails.")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "prepare"
    ensure_dir(out_dir)
    results_path = out_dir / "results.json"
    log_path = out_dir / "log.txt"

    results = build_results_template(stage="prepare", task="download", command=" ".join([shlex_quote(sys.executable)] + [shlex_quote(a) for a in sys.argv]))
    results["meta"]["env_vars"] = {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_")}
    results["meta"]["decision_reason"] = (
        "Chose Event2012 (README demo dataset) and SBERT model "
        f"('{args.model_id}', referenced by SocialED.detector.SBERT) for minimal reproducible runs."
    )

    # Respect write constraints: keep all caches under benchmark_assets/cache.
    cache_root = root / "benchmark_assets" / "cache"
    ensure_dir(cache_root)
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_root / "xdg"))
    os.environ.setdefault("HF_HOME", str(cache_root / "huggingface"))
    os.environ.setdefault("HF_HUB_CACHE", str(cache_root / "huggingface" / "hub"))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(cache_root / "huggingface" / "transformers"))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(cache_root / "sentence_transformers"))
    os.environ.setdefault("TORCH_HOME", str(cache_root / "torch"))
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    # Ensure report exists early; if missing, do not attempt downloads that may require the bench env.
    report_path = resolve_report_path(args.report_path)
    report, report_err = load_report(report_path)
    if report_err:
        results["status"] = "failure"
        results["exit_code"] = 1
        results["failure_category"] = "missing_report"
        results["error_excerpt"] = f"Report load failed: {report_err} ({report_path})"
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    dataset_name = args.dataset
    dataset_repo_url = args.dataset_repo
    model_id = args.model_id

    # Dataset download (GitHub repo clone to cache, then copy out the .npy).
    dataset_dst_dir = root / "benchmark_assets" / "dataset" / dataset_name
    ensure_dir(dataset_dst_dir)
    dataset_file = dataset_dst_dir / f"{dataset_name}.npy"
    subset_file = dataset_dst_dir / f"{dataset_name}_subset.npy"

    dataset_version = ""
    dataset_source = f"{dataset_repo_url}#{dataset_name}"
    dataset_error: Optional[str] = None

    env = dict(os.environ)
    datasets_repo_cache = cache_root / "socialed_datasets_repo"
    if not datasets_repo_cache.exists():
        rc, out = run_cmd(["git", "clone", "--depth", "1", dataset_repo_url, str(datasets_repo_cache)], cwd=root, env=env)
        with log_path.open("a", encoding="utf-8") as log:
            log.write("[prepare] git clone SocialED_datasets\n")
            log.write(out + "\n")
        if rc != 0:
            dataset_error = f"git_clone_failed rc={rc}"
    if dataset_error is None and datasets_repo_cache.exists():
        rc, out = run_cmd(["git", "-C", str(datasets_repo_cache), "rev-parse", "HEAD"], cwd=root, env=env)
        if rc == 0:
            dataset_version = out.strip()
        found = find_dataset_npy(datasets_repo_cache, dataset_name)
        if not found:
            dataset_error = f"{dataset_name}.npy not found in cached repo {datasets_repo_cache}"
        else:
            if not dataset_file.exists():
                shutil.copy2(found, dataset_file)
            else:
                # If already present, keep it (offline reuse).
                pass

    # Create subset file (preferred for minimal runs).
    if dataset_error is None:
        try:
            import numpy as np
            import pandas as pd

            data = np.load(dataset_file, allow_pickle=True)
            df = pd.DataFrame(data, columns=REQUIRED_DATASET_COLUMNS)
            subset_df = df.head(max(2, int(args.subset_size))).copy()
            subset_arr = subset_df[REQUIRED_DATASET_COLUMNS].to_numpy()
            np.save(subset_file, subset_arr, allow_pickle=True)
        except Exception as e:
            dataset_error = f"dataset_subset_failed: {e}"

    # Model download (HF hub) -> cache, then symlink under benchmark_assets/model.
    model_dst_dir = root / "benchmark_assets" / "model"
    ensure_dir(model_dst_dir)
    model_link = model_dst_dir / "sbert_model"

    model_version = ""
    model_error: Optional[str] = None
    model_source = f"https://huggingface.co/{model_id}"
    model_snapshot_path: Optional[Path] = None

    if model_link.exists() or model_link.is_symlink():
        # Reuse existing link.
        try:
            resolved = model_link.resolve(strict=True)
            model_snapshot_path = resolved
            model_version = _infer_hf_snapshot_version(resolved)
        except Exception:
            model_error = "existing_model_link_invalid"

    if model_error is None and model_snapshot_path is None:
        model_snapshot_path, model_version, model_error = snapshot_download_model(model_id, cache_root=cache_root, offline_ok=args.offline_ok)

    if model_error is None and model_snapshot_path is not None:
        if not model_snapshot_path.exists():
            model_error = f"resolved_model_dir_missing: {model_snapshot_path}"
        else:
            try:
                if model_link.exists() or model_link.is_symlink():
                    model_link.unlink()
                model_link.symlink_to(model_snapshot_path, target_is_directory=True)
            except Exception:
                # Fall back to copying a minimal pointer file, but keep assets.model.path pointing to cache dir.
                pass

    # Finalize results.
    if dataset_error is None:
        results["assets"]["dataset"]["path"] = str(subset_file.relative_to(root))
        results["assets"]["dataset"]["source"] = dataset_source
        results["assets"]["dataset"]["version"] = dataset_version
        results["assets"]["dataset"]["sha256"] = sha256_file(subset_file)
    else:
        results["assets"]["dataset"]["path"] = str(dataset_file.relative_to(root)) if dataset_file.exists() else ""
        results["assets"]["dataset"]["source"] = dataset_source
        results["assets"]["dataset"]["version"] = dataset_version

    if model_error is None and model_snapshot_path is not None:
        results["assets"]["model"]["path"] = str(model_link.relative_to(root)) if model_link.exists() else str(model_snapshot_path.relative_to(root))
        results["assets"]["model"]["source"] = model_source
        results["assets"]["model"]["version"] = model_version
        # Hash the resolved directory to avoid assuming hub cache layout.
        results["assets"]["model"]["sha256"] = sha256_dir(model_snapshot_path)
    else:
        results["assets"]["model"]["source"] = model_source
        results["assets"]["model"]["version"] = model_version

    if dataset_error or model_error:
        results["status"] = "failure"
        results["exit_code"] = 1
        if dataset_error and "clone" in dataset_error:
            results["failure_category"] = "download_failed"
        elif model_error and "snapshot_download" in model_error:
            results["failure_category"] = "download_failed"
        elif model_error:
            results["failure_category"] = "model"
        else:
            results["failure_category"] = "data"
        msg = f"dataset_error={dataset_error} model_error={model_error}"
        with log_path.open("a", encoding="utf-8") as log:
            log.write("[prepare] FAILURE: " + msg + "\n")
        results["error_excerpt"] = msg + "\n" + tail_text(log_path)
    else:
        results["status"] = "success"
        results["exit_code"] = 0
        results["skip_reason"] = "not_applicable"
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""

    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if results["status"] in ("success", "skipped") else 1


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


if __name__ == "__main__":
    raise SystemExit(main())

