from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple

_RE_GITHUB = re.compile(r"github\.com[:/]+(?P<owner>[^/\s]+)/(?P<repo>[^/\s#]+)", re.I)

def parse_repo_full_name(repo_url: str) -> Optional[str]:
    if not repo_url:
        return None
    m = _RE_GITHUB.search(str(repo_url).strip())
    if not m:
        return None
    owner = m.group("owner")
    repo = re.sub(r"\.git$", "", m.group("repo"))
    return f"{owner}/{repo}"

def normalize_commit_sha(sha: Any) -> Optional[str]:
    if sha is None:
        return None
    s = str(sha).strip()
    if not s:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", s):
        return None
    return s.lower()

def parse_bool(val: Any) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in {"1","true","t","yes","y","是","可用","ok"}:
        return True
    if s in {"0","false","f","no","n","否","不可用"}:
        return False
    return None

def parse_eval_dims(val: Any) -> List[str]:
    if val is None:
        return []
    s = str(val).strip()
    if not s:
        return []
    parts = re.split(r"[,，;/\s|]+", s)
    dims: List[str] = []
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        m = re.fullmatch(r"c([0-5])", p)
        if m:
            dims.append(f"c{m.group(1)}")
    out: List[str] = []
    for d in dims:
        if d not in out:
            out.append(d)
    return out

def parse_repo_type(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    m = re.search(r"\b([A-Za-z])\b", s)
    if m:
        return m.group(1).upper()
    if len(s) == 1 and s.isalpha():
        return s.upper()
    return s

def parse_hardware_bucket(val: Any) -> Tuple[str, Dict[str, Any]]:
    '''
    Map xlsx 的 "CPU/multi GPU" 描述到调度桶：
      - cpu / single / multi

    同时返回更细的 tags 供后续筛选（例如 needs_cuda / min_gpus）。
    '''
    s = "" if val is None else str(val).strip()
    s_low = s.lower()
    tags: Dict[str, Any] = {"raw": s}
    bucket = "cpu"

    if "multi" in s_low:
        bucket = "multi"
        tags["min_gpus"] = 2
        tags["needs_cuda"] = True
    elif "gpu" in s_low or "cuda" in s_low or "single" in s_low:
        bucket = "single"
        tags["min_gpus"] = 1
        tags["needs_cuda"] = True if ("cuda" in s_low or "gpu" in s_low) else None
    else:
        bucket = "cpu"
        tags["min_gpus"] = 0
        tags["needs_cuda"] = False

    tags["supports_cpu"] = ("cpu" in s_low) if s else None
    return bucket, tags

def split_csv(val: Any) -> List[str]:
    if val is None:
        return []
    s = str(val).strip()
    if not s:
        return []
    return [p.strip() for p in re.split(r"[,，;/\s]+", s) if p.strip()]
