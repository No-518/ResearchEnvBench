# m5/utils.py
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_get(d: Any, *keys: str, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def parse_repo_full_name(repo_url: str) -> str:
    # supports:
    # - https://github.com/owner/repo.git
    # - git@github.com:owner/repo.git
    # - https://github.com/owner/repo
    s = (repo_url or "").strip()
    if not s:
        return ""
    # git@github.com:owner/repo.git
    m = re.search(r"github\.com[:/](?P<name>[^/]+/[^/]+?)(?:\.git)?$", s)
    if m:
        return m.group("name")
    return s


def parse_nvidia_smi_text(txt: str) -> Tuple[str, str, str]:
    """
    Return (gpu_name, driver_version, cuda_runtime)
    Best-effort regex parse from:
      NVIDIA-SMI 535.104.05 Driver Version: 535.104.05 CUDA Version: 12.2
      GPU 0: NVIDIA GeForce RTX 4090 (UUID: ...)
    """
    gpu_name = ""
    driver_ver = ""
    cuda_ver = ""

    if not txt:
        return gpu_name, driver_ver, cuda_ver

    # Driver/CUDA line
    m = re.search(r"Driver Version:\s*([0-9.]+)", txt)
    if m:
        driver_ver = m.group(1).strip()
    m = re.search(r"CUDA Version:\s*([0-9.]+)", txt)
    if m:
        cuda_ver = m.group(1).strip()

    # GPU name from -L output or listing lines
    # GPU 0: NVIDIA GeForce RTX 4090 (UUID: ...)
    m = re.search(r"GPU\s+0:\s*([^(]+)\(", txt)
    if m:
        gpu_name = m.group(1).strip()
    else:
        # sometimes "Product Name" appears
        m = re.search(r"Product Name\s*:\s*(.+)", txt)
        if m:
            gpu_name = m.group(1).strip()

    return gpu_name, driver_ver, cuda_ver


def try_read_text(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def normalize_status(s: Any) -> str:
    """
    Normalize status strings across scripts: success/failed/skipped/missing/unknown.
    """
    if s is None:
        return ""
    t = str(s).strip().lower()
    if t in ("success", "ok", "passed", "pass"):
        return "success"
    if t in ("failed", "failure", "error", "fail"):
        return "failed"
    if t in ("skipped", "skip"):
        return "skipped"
    if t in ("missing",):
        return "missing"
    return t or "unknown"


def extract_token_total(agent_dir: Path) -> Optional[int]:
    """
    Best-effort token extraction.
    Priority:
      1) agent/langfuse_usage.json with fields like { "token_total": 123 } or { "total_tokens": 123 }
      2) agent/trace.json similar
      3) agent/run_metadata.json (usually no tokens; keep as future hook)
    """
    candidates = [
        agent_dir / "langfuse_usage.json",
        agent_dir / "langfuse_trace.json",
        agent_dir / "trace.json",
    ]
    for p in candidates:
        j = read_json(p)
        if not j:
            continue
        for k in ("token_total", "total_tokens", "tokens_total"):
            v = j.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)

        # sometimes nested like usage.total_tokens
        v = safe_get(j, "usage", "total_tokens")
        if isinstance(v, int):
            return v

    # no token file
    return None
