"""
Benchmark-only runtime patches.

This file is imported automatically by Python when it is discoverable on PYTHONPATH.
We only apply patches when explicitly enabled via env vars, so regular repo usage
is unaffected.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _patch_aim_attnprobe_return_tuple() -> None:
    """
    AIMv1's main_attnprobe.py expects `model(...)` to return `(logits, features)`,
    but AIMMixin.forward returns only `logits` in this repo version.

    This patch makes AIMMixin.forward return `(logits, None)` when it would
    otherwise return a single object, matching the entrypoint expectation without
    changing logits computation.
    """

    # Ensure local AIMv1 is importable even when running from repo root.
    repo_root = _repo_root()
    aim_v1 = repo_root / "aim-v1"
    if aim_v1.is_dir():
        sys.path.insert(0, str(aim_v1))

    try:
        from aim.v1 import mixins as aim_mixins  # type: ignore
    except Exception:
        return

    forward = getattr(aim_mixins.AIMMixin, "forward", None)
    if forward is None:
        return

    if getattr(forward, "_scimlopsbench_patched", False):
        return

    orig_forward = forward

    def patched_forward(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        out = orig_forward(self, *args, **kwargs)
        if isinstance(out, tuple) and len(out) == 2:
            return out
        return out, None

    setattr(patched_forward, "_scimlopsbench_patched", True)
    aim_mixins.AIMMixin.forward = patched_forward  # type: ignore[assignment]


if os.environ.get("SCIMLOPSBENCH_AIM_ATTNPROBE_COMPAT", "") == "1":
    _patch_aim_attnprobe_return_tuple()

