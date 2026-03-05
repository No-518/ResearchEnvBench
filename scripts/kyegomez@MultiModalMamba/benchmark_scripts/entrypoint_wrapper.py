#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path


def _parse_rank(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Execute a repo entrypoint under an explicit CPU/CUDA device context.")
    ap.add_argument("--entrypoint", required=True, help="Repo entrypoint script path (e.g., example.py)")
    ap.add_argument("--device", required=True, choices=["cpu", "cuda"])
    ap.add_argument("--local-rank", type=int, default=None, help="Override LOCAL_RANK for cuda device selection")
    args = ap.parse_args(argv)

    entrypoint = Path(args.entrypoint).resolve()
    if not entrypoint.is_file():
        print(f"[entrypoint_wrapper] entrypoint_not_found: {entrypoint}", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    for p in (str(repo_root), str(entrypoint.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)

    if args.device == "cpu":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    else:
        try:
            import torch  # type: ignore
        except Exception as e:
            print(f"[entrypoint_wrapper] torch_import_failed: {e}", file=sys.stderr)
            return 2

        if not torch.cuda.is_available():
            print("[entrypoint_wrapper] cuda_not_available", file=sys.stderr)
            return 2

        rank = args.local_rank
        if rank is None:
            rank = _parse_rank(os.environ.get("LOCAL_RANK") or os.environ.get("RANK") or "0")

        torch.cuda.set_device(rank)
        if hasattr(torch, "set_default_device"):
            torch.set_default_device(f"cuda:{rank}")
        else:
            torch.set_default_tensor_type(torch.cuda.FloatTensor)

    runpy.run_path(str(entrypoint), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
