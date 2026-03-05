#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REQUIRED_COLUMNS = [
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


def load_prepare_assets(prepare_results_path: Path) -> Dict[str, Any]:
    data = json.loads(prepare_results_path.read_text(encoding="utf-8"))
    assets = data.get("assets", {})
    if not isinstance(assets, dict):
        raise ValueError("prepare.results.json missing assets object")
    return assets


def _resolve_asset_path(root: Path, p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return root / path


def _load_dataset_subset(dataset_path: Path) -> "pandas.DataFrame":
    import numpy as np
    import pandas as pd

    arr = np.load(dataset_path, allow_pickle=True)
    df = pd.DataFrame(arr, columns=REQUIRED_COLUMNS)
    # Ensure minimal columns for SBERT.
    if "filtered_words" not in df.columns or "event_id" not in df.columns:
        raise ValueError("dataset missing required columns: filtered_words/event_id")
    return df


class BenchDataset:
    def __init__(self, df, language: str = "English"):
        self._df = df
        self._language = language

    def load_data(self):
        return self._df

    def get_dataset_language(self):
        return self._language

    def get_dataset_name(self):
        return "benchmark_subset"


def _maybe_set_device_for_distributed() -> Tuple[Optional[int], Optional[int]]:
    # torchrun sets LOCAL_RANK and WORLD_SIZE.
    local_rank = os.environ.get("LOCAL_RANK")
    world_size = os.environ.get("WORLD_SIZE")
    try:
        lr = int(local_rank) if local_rank is not None else None
    except Exception:
        lr = None
    try:
        ws = int(world_size) if world_size is not None else None
    except Exception:
        ws = None

    if lr is None:
        return None, ws

    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.set_device(lr)
    except Exception:
        # If torch import fails, let caller fail naturally later.
        pass
    return lr, ws


def _load_sbert_class(root: Path):
    """
    Load SBERT without importing `SocialED.detector` package.

    `SocialED/detector/__init__.py` imports many detectors, some of which require optional
    heavyweight deps (e.g., spaCy `en_core_web_lg`). Importing the package can fail even
    when the SBERT detector itself is usable.
    """
    sbert_path = root / "SocialED" / "detector" / "sbert.py"
    if sbert_path.exists():
        # Ensure SocialED/ is on sys.path so `dataset.*` imports inside sbert.py resolve
        # deterministically to this repository.
        socialed_root = root / "SocialED"
        if str(socialed_root) not in sys.path:
            sys.path.insert(0, str(socialed_root))

        spec = importlib.util.spec_from_file_location("socialed_detector_sbert", str(sbert_path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to create import spec for: {sbert_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "SBERT"):
            raise RuntimeError(f"SBERT class not found in module: {sbert_path}")
        return getattr(module, "SBERT")

    # Fallback: try regular import (may fail if optional deps are missing).
    from SocialED.detector import SBERT  # type: ignore

    return SBERT


def main() -> int:
    ap = argparse.ArgumentParser(description="Minimal SocialED SBERT smoke test using prepared assets.")
    ap.add_argument("--prepare-results", default="build_output/prepare/results.json")
    ap.add_argument("--max-samples", type=int, default=12)
    ap.add_argument("--artifact-dir", default="", help="Optional directory to write per-rank artifacts.")
    ap.add_argument("--distributed", action="store_true", help="Enable torchrun-friendly behavior (per-rank slicing).")
    ap.add_argument(
        "--require-gpu-count",
        type=int,
        default=0,
        help="If >0, require torch.cuda.device_count() >= N and fail otherwise.",
    )
    args = ap.parse_args()

    root = repo_root()
    prepare_results_path = _resolve_asset_path(root, args.prepare_results)
    if not prepare_results_path.exists():
        sys.stderr.write(f"[socialed_sbert_smoketest] prepare results not found: {prepare_results_path}\n")
        return 1

    assets = load_prepare_assets(prepare_results_path)
    dataset_asset = assets.get("dataset", {})
    model_asset = assets.get("model", {})

    dataset_path_raw = str(dataset_asset.get("path", ""))
    model_path_raw = str(model_asset.get("path", ""))
    if not dataset_path_raw:
        sys.stderr.write("[socialed_sbert_smoketest] dataset asset path missing\n")
        return 1
    if not model_path_raw:
        sys.stderr.write("[socialed_sbert_smoketest] model asset path missing\n")
        return 1

    dataset_path = _resolve_asset_path(root, dataset_path_raw)
    model_path = _resolve_asset_path(root, model_path_raw)
    if not dataset_path.exists():
        sys.stderr.write(f"[socialed_sbert_smoketest] dataset path missing: {dataset_path}\n")
        return 1
    if not model_path.exists():
        sys.stderr.write(f"[socialed_sbert_smoketest] model path missing: {model_path}\n")
        return 1

    local_rank, world_size = (None, None)
    if args.distributed:
        local_rank, world_size = _maybe_set_device_for_distributed()

    if args.require_gpu_count and args.require_gpu_count > 0:
        try:
            import torch

            gpu_count = int(torch.cuda.device_count())
            if not torch.cuda.is_available() or gpu_count < int(args.require_gpu_count):
                sys.stderr.write(
                    f"[socialed_sbert_smoketest] require_gpus={args.require_gpu_count} but cuda_available={torch.cuda.is_available()} gpu_count={gpu_count}\n"
                )
                return 1
        except Exception as e:
            sys.stderr.write(f"[socialed_sbert_smoketest] GPU requirement check failed: {e}\n")
            return 1

    # Load dataset subset.
    df = _load_dataset_subset(dataset_path)
    max_samples = max(2, int(args.max_samples))

    if args.distributed and world_size and world_size > 1 and local_rank is not None:
        # Slice so each rank gets its own subset while remaining large enough for train/test split.
        df = df.iloc[local_rank::world_size].copy()
        df = df.head(max_samples).copy()
    else:
        df = df.head(max_samples).copy()

    # Run SBERT detector (inference-style pipeline).
    SBERT = _load_sbert_class(root)

    dataset_obj = BenchDataset(df, language="English")
    detector = SBERT(dataset_obj, model_name=str(model_path))

    processed = detector.preprocess()
    # Ensure small, deterministic workload.
    detector.df = processed.head(max_samples).copy()

    gt, preds = detector.detection()
    _ = detector.evaluate(gt, preds)

    # Optional artifact write.
    if args.artifact_dir:
        art_dir = _resolve_asset_path(root, args.artifact_dir)
        art_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp_utc": utc_timestamp(),
            "local_rank": local_rank,
            "world_size": world_size,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            "dataset_rows": int(len(detector.df)) if detector.df is not None else None,
        }
        out_path = art_dir / (f"rank{local_rank}.json" if local_rank is not None else "run.json")
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
