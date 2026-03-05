#!/usr/bin/env python3
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _extract_flag_value(argv: List[str], names: Tuple[str, ...]) -> Tuple[List[str], Optional[str]]:
    """Remove `--flag value` / `--flag=value` from argv and return (new_argv, value)."""
    out: List[str] = []
    value: Optional[str] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        matched = False
        for name in names:
            if tok == name:
                if i + 1 < len(argv):
                    value = argv[i + 1]
                    i += 2
                    matched = True
                    break
            if tok.startswith(name + "="):
                value = tok.split("=", 1)[1]
                i += 1
                matched = True
                break
        if matched:
            continue
        out.append(tok)
        i += 1
    return out, value


def _rewrite_checkpoint_dir(argv: List[str], *, local_rank: str) -> List[str]:
    if local_rank == "0":
        return argv

    out = list(argv)
    for i, tok in enumerate(out):
        if tok == "--checkpoint_dir" and i + 1 < len(out):
            base = out[i + 1]
            new_dir = str(Path(base) / f"rank{local_rank}")
            Path(new_dir).mkdir(parents=True, exist_ok=True)
            out[i + 1] = new_dir
            return out
        if tok.startswith("--checkpoint_dir="):
            base = tok.split("=", 1)[1]
            new_dir = str(Path(base) / f"rank{local_rank}")
            Path(new_dir).mkdir(parents=True, exist_ok=True)
            out[i] = f"--checkpoint_dir={new_dir}"
            return out
    return out


def _resolve_train_entrypoint() -> Path:
    cwd = Path.cwd()
    candidate = cwd / "train.py"
    if candidate.is_file():
        return candidate

    repo_root = Path(__file__).resolve().parent.parent
    candidate = repo_root / "train" / "speech_enhancement" / "train.py"
    return candidate


def main() -> int:
    argv = sys.argv[1:]

    # torch.distributed.run / torchrun typically passes `--local_rank`; the repo's train.py expects `--local-rank`.
    argv, local_rank_from_cli = _extract_flag_value(argv, ("--local_rank", "--local-rank"))
    local_rank = (
        str(local_rank_from_cli).strip()
        if local_rank_from_cli is not None
        else str(os.environ.get("LOCAL_RANK", "")).strip()
    )
    local_rank = local_rank if local_rank else "0"

    argv = _rewrite_checkpoint_dir(argv, local_rank=local_rank)

    train_argv = ["--local-rank", local_rank, *argv]
    train_py = _resolve_train_entrypoint()
    train_dir = train_py.parent
    os.chdir(train_dir)
    sys.path.insert(0, str(train_dir))
    sys.argv = [str(train_py), *train_argv]
    runpy.run_path(str(train_py), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
