"""
Benchmark-only runtime patches.

This file is automatically imported by Python when it is present on PYTHONPATH
as `sitecustomize`. We use it to enforce benchmark constraints (batch_size=1,
max_steps=1, CPU forcing) while still invoking the repository's native
entrypoints.
"""

from __future__ import annotations

import os


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y"}


def _patch_force_cpu_env() -> None:
    if not _env_flag("SCIMLOPSBENCH_FORCE_CPU"):
        return

    # Ensure the repo entrypoint cannot override CPU forcing via CUDA/XPU env vars.
    original_setitem = os.environ.__class__.__setitem__

    def patched_setitem(self, key: str, value: str) -> None:  # type: ignore[override]
        if key in {"CUDA_VISIBLE_DEVICES", "XPU_VISIBLE_DEVICES"}:
            return original_setitem(self, key, "")
        return original_setitem(self, key, value)

    os.environ.__class__.__setitem__ = patched_setitem  # type: ignore[assignment]
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["XPU_VISIBLE_DEVICES"] = ""


def _patch_embedding_cap() -> None:
    cap_raw = os.environ.get("SCIMLOPSBENCH_EMBEDDING_CAP", "").strip()
    if not cap_raw:
        return
    try:
        cap = int(cap_raw)
    except Exception:
        return
    if cap <= 0:
        return

    try:
        import torch.nn as nn
    except Exception:
        return

    original_init = nn.Embedding.__init__

    def patched_init(self, num_embeddings, embedding_dim, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            if int(num_embeddings) > cap:
                num_embeddings = cap
        except Exception:
            pass
        return original_init(self, num_embeddings, embedding_dim, *args, **kwargs)

    nn.Embedding.__init__ = patched_init  # type: ignore[assignment]


def _patch_max_steps() -> None:
    max_steps_raw = os.environ.get("SCIMLOPSBENCH_MAX_STEPS", "").strip()
    if not max_steps_raw:
        return
    try:
        max_steps = int(max_steps_raw)
    except Exception:
        return
    if max_steps <= 0:
        return

    try:
        from torchft.manager import Manager
    except Exception:
        return

    original_current_step = Manager.current_step

    def patched_current_step(self) -> int:  # type: ignore[override]
        step = original_current_step(self)
        if step >= max_steps:
            # Make entrypoints with ">= huge" exit guards terminate immediately.
            return 10**9
        return step

    Manager.current_step = patched_current_step  # type: ignore[assignment]


def _patch_torchvision_cifar10() -> None:
    dataset_dir = os.environ.get("SCIMLOPSBENCH_DATASET_DIR", "").strip()
    if not dataset_dir:
        return

    try:
        import torchvision.datasets as tv_datasets
    except Exception:
        return

    if not hasattr(tv_datasets, "CIFAR10"):
        return

    original_cifar10 = tv_datasets.CIFAR10

    def patched_cifar10(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["root"] = dataset_dir
        # Always prefer offline reuse; torchvision will error if files are absent.
        kwargs["download"] = False
        return original_cifar10(*args, **kwargs)

    tv_datasets.CIFAR10 = patched_cifar10  # type: ignore[assignment]


def _patch_torchdata_stateful_dataloader() -> None:
    bs_raw = os.environ.get("SCIMLOPSBENCH_BATCH_SIZE", "").strip()
    nw_raw = os.environ.get("SCIMLOPSBENCH_NUM_WORKERS", "").strip()

    if not bs_raw and not nw_raw:
        return

    try:
        import torchdata.stateful_dataloader as sdl
    except Exception:
        return

    if not hasattr(sdl, "StatefulDataLoader"):
        return

    cls = sdl.StatefulDataLoader
    original_init = cls.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if bs_raw:
            try:
                kwargs["batch_size"] = int(bs_raw)
            except Exception:
                pass
        if nw_raw:
            try:
                kwargs["num_workers"] = int(nw_raw)
            except Exception:
                pass
        return original_init(self, *args, **kwargs)

    cls.__init__ = patched_init  # type: ignore[assignment]


def _enforce_cuda_requirements() -> None:
    require_cuda = _env_flag("SCIMLOPSBENCH_REQUIRE_CUDA")
    min_gpu_raw = os.environ.get("SCIMLOPSBENCH_REQUIRE_MIN_GPU_COUNT", "").strip()
    if not require_cuda and not min_gpu_raw:
        return

    try:
        import torch
    except Exception:
        if require_cuda or min_gpu_raw:
            raise RuntimeError("SCIMLOPSBENCH_REQUIRE_CUDA set but torch import failed")
        return

    if require_cuda and not torch.cuda.is_available():
        raise RuntimeError("CUDA required for this stage but torch.cuda.is_available() is False")

    if min_gpu_raw:
        try:
            min_gpu = int(min_gpu_raw)
        except Exception:
            min_gpu = 0
        if min_gpu > 0:
            count = torch.cuda.device_count()
            if count < min_gpu:
                raise RuntimeError(
                    f"Need >= {min_gpu} GPUs for this stage, but torch.cuda.device_count() == {count}"
                )


def _apply() -> None:
    _patch_force_cpu_env()
    _patch_embedding_cap()
    _patch_max_steps()
    _patch_torchvision_cifar10()
    _patch_torchdata_stateful_dataloader()
    _enforce_cuda_requirements()


try:
    _apply()
except Exception:
    # Avoid breaking the entrypoint if any optional patch fails.
    pass
