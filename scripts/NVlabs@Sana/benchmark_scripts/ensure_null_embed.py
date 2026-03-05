#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a minimal null-embed .pth file expected by Sana checkpoints.")
    ap.add_argument("--path", required=True, type=Path, help="Output .pth path")
    ap.add_argument("--max-length", required=True, type=int)
    ap.add_argument("--hidden-size", required=True, type=int)
    args = ap.parse_args(argv)

    try:
        import torch
    except Exception as e:
        print(f"[ensure_null_embed] failed to import torch: {e}", file=sys.stderr)
        return 1

    out_path: Path = args.path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        return 0

    max_len = int(args.max_length)
    hidden = int(args.hidden_size)

    payload = {
        "uncond_prompt_embeds": torch.zeros(1, max_len, hidden),
        "uncond_prompt_embeds_mask": torch.ones(1, max_len, dtype=torch.long),
    }
    try:
        torch.save(payload, str(out_path))
    except Exception as e:
        print(f"[ensure_null_embed] failed to write {out_path}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

