#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Rewrite run_matrix baseline values and regenerate stable job_id.

Why this exists:
- Public release includes prebuilt matrices for some backends.
- Reproducing additional backends (for example `claude_code`) should not require
  manually editing JSONL files.
- If baseline changes, job_id should change too to avoid collisions across runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, Iterable, List


def stable_job_id(repo_full_name: str, commit_sha: str, baseline: str) -> str:
    raw = f"{repo_full_name}@{commit_sha}::{baseline}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def load_jsonl(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        obj = json.loads(s)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{ln} is not a JSON object")
        rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rewrite_rows(rows: List[Dict], baseline: str) -> List[Dict]:
    out: List[Dict] = []
    for r in rows:
        nr = dict(r)
        nr["baseline"] = baseline
        repo_full_name = str(nr.get("repo_full_name") or "").strip()
        commit_sha = str(nr.get("commit_sha") or "").strip()
        if repo_full_name and commit_sha:
            nr["job_id"] = stable_job_id(repo_full_name, commit_sha, baseline)
        out.append(nr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Rewrite baseline in run_matrix JSONL and regenerate job_id.")
    ap.add_argument("--source", type=Path, required=True, help="Source run_matrix.jsonl")
    ap.add_argument("--baseline", required=True, help="New baseline value, e.g. claude_code")
    ap.add_argument("--out", type=Path, required=True, help="Output run_matrix.jsonl")
    ap.add_argument(
        "--smoke-repo",
        default="",
        help="Optional repo_full_name for smoke output, e.g. Auto1111SDK/Auto1111SDK",
    )
    ap.add_argument(
        "--smoke-out",
        type=Path,
        default=None,
        help="Optional smoke output path. If omitted and --smoke-repo is set, defaults to <out stem>_smoke<suffix>.",
    )
    args = ap.parse_args()

    src = args.source.resolve()
    out = args.out.resolve()

    rows = load_jsonl(src)
    rewritten = rewrite_rows(rows, args.baseline)
    write_jsonl(out, rewritten)
    print(f"[rewrite] source={src}")
    print(f"[rewrite] out={out} rows={len(rewritten)} baseline={args.baseline}")

    smoke_repo = args.smoke_repo.strip()
    if smoke_repo:
        smoke = [r for r in rewritten if str(r.get("repo_full_name") or "") == smoke_repo]
        if not smoke:
            raise RuntimeError(f"smoke repo not found in source rows: {smoke_repo}")
        smoke_out = args.smoke_out.resolve() if args.smoke_out else out.with_name(f"{out.stem}_smoke{out.suffix}")
        write_jsonl(smoke_out, smoke)
        print(f"[rewrite] smoke_out={smoke_out} rows={len(smoke)} repo={smoke_repo}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
