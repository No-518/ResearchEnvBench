#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Generate per-model run_matrix files for NexAU-based model backends."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List


MODEL_BASELINES: Dict[str, str] = {
    "deepseek31_nexn1": "nexau_deepseek31_nexn1",
    "gemini30": "nexau_gemini30",
    "claude_sonnet45": "nexau_claude_sonnet45",
    "minimax25": "nexau_minimax25",
}


def load_jsonl(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        obj = json.loads(s)
        if not isinstance(obj, dict):
            raise ValueError(f"line {ln} is not a JSON object")
        rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rewrite_baseline(rows: List[Dict], baseline: str) -> List[Dict]:
    out: List[Dict] = []
    for r in rows:
        nr = dict(r)
        nr["baseline"] = baseline
        out.append(nr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source",
        type=Path,
        default=Path("m1_repo_manifest_module/manifests/run_matrix.jsonl"),
        help="source run_matrix.jsonl",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("m1_repo_manifest_module/manifests"),
        help="output directory for generated matrix files",
    )
    ap.add_argument(
        "--smoke-repo",
        default="Auto1111SDK/Auto1111SDK",
        help="repo_full_name used in smoke matrices",
    )
    args = ap.parse_args()

    source = args.source.resolve()
    out_dir = args.out_dir.resolve()
    source_rows = load_jsonl(source)
    smoke_rows_src = [r for r in source_rows if str(r.get("repo_full_name") or "") == args.smoke_repo]
    if not smoke_rows_src:
        raise RuntimeError(f"smoke repo not found in source matrix: {args.smoke_repo}")

    print(f"[matrix] source={source}")
    print(f"[matrix] source_rows={len(source_rows)} smoke_repo={args.smoke_repo} smoke_rows={len(smoke_rows_src)}")

    for model_name, baseline in MODEL_BASELINES.items():
        full_rows = rewrite_baseline(source_rows, baseline)
        smoke_rows = rewrite_baseline(smoke_rows_src, baseline)

        full_path = out_dir / f"run_matrix_{baseline}.jsonl"
        smoke_path = out_dir / f"run_matrix_smoke_{baseline}.jsonl"

        write_jsonl(full_path, full_rows)
        write_jsonl(smoke_path, smoke_rows)

        print(f"[matrix] wrote {full_path} ({len(full_rows)} rows)")
        print(f"[matrix] wrote {smoke_path} ({len(smoke_rows)} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
