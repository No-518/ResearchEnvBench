"""
Runtime patches for running this repository's native entrypoints without editing repo files.

Applied automatically by Python when this directory is added to PYTHONPATH.
"""

from __future__ import annotations


def _patch_attn_type_alias() -> None:
    try:
        from yunchang.kernels import AttnType  # type: ignore
    except Exception:
        return

    try:
        orig_func = AttnType.from_string.__func__  # type: ignore[attr-defined]
    except Exception:
        return

    def patched_from_string(cls, s: str):  # type: ignore[no-redef]
        # Repo CLI uses `--attn_type torch` (see benchmark/benchmark_longctx.py),
        # but AttnType values are `torch_*`. Map "torch" to a torch-based backend.
        if s == "torch":
            return cls.TORCH_EFFICIENT
        return orig_func(cls, s)

    AttnType.from_string = classmethod(patched_from_string)  # type: ignore[assignment]


_patch_attn_type_alias()

