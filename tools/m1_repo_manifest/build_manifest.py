#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import openpyxl
except Exception:
    openpyxl = None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.m1_repo_manifest.lib.manifest import build_manifest, save_manifest, expand_runs, save_run_matrix


def _xlsx_has_hw_bucket_column(xlsx_path: str, sheet: str | None) -> bool:
    """Return True if header row appears to contain a hardware bucket column."""
    if openpyxl is None:
        return False
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True)
        ws = wb[sheet] if sheet else wb.active
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        header = [(str(c).strip().lower() if c is not None else "") for c in header_row]
        hw_cols = {"hardware_bucket", "hw_bucket", "bucket", "hardware", "gpu_bucket"}
        return not hw_cols.isdisjoint(set(header))
    except Exception:
        # best-effort; don't break M1 on header parsing issues
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="M1: build manifest.json from repos.xlsx")
    p.add_argument("--xlsx", required=True, help="path to repos.xlsx")
    p.add_argument("--sheet", default=None, help="xlsx sheet name (optional)")
    p.add_argument("--out", required=True, help="output manifest.json")
    p.add_argument(
        "--default-baselines",
        default="nex,codex,claude_code",
        help="comma separated list; injected into baseline_targets (unless row overrides)",
    )
    p.add_argument("--include-unusable", action="store_true", help="include rows marked unusable")
    p.add_argument("--emit-run-matrix", default=None, help="optional path to write run_matrix.jsonl")

    # optional filters to generate a smaller manifest/run list
    p.add_argument("--filter-hardware", default=None, help="comma separated: cpu,single,multi,auto")
    p.add_argument("--filter-regex", default=None, help="only keep repos whose repo_full_name matches regex")
    p.add_argument("--strict", action="store_true", help="fail if any kept row has invalid repo_full_name/commit_sha")
    args = p.parse_args()

    default_baselines = [b.strip() for b in args.default_baselines.split(",") if b.strip()]

    # If the input table does NOT contain a hardware bucket column, default to "auto"
    # so downstream can decide CPU/GPU based on host availability.
    has_hw_col = _xlsx_has_hw_bucket_column(args.xlsx, args.sheet)
    force_auto_hw = not has_hw_col

    manifest = build_manifest(
        xlsx_path=args.xlsx,
        sheet=args.sheet,
        default_baselines=default_baselines,
        include_unusable=args.include_unusable,
    )

    if force_auto_hw:
        for r in manifest.get("repos", []):
            r["hardware_bucket"] = "auto"
            tags = r.get("hardware_tags") or {}
            tags.setdefault("raw", "auto")
            r["hardware_tags"] = tags

    # apply filters on manifest['repos']
    hardware_allow = None
    if args.filter_hardware:
        hardware_allow = {x.strip() for x in args.filter_hardware.split(",") if x.strip()}

    rx = re.compile(args.filter_regex) if args.filter_regex else None

    filtered = []
    invalid = []
    for r in manifest["repos"]:
        if hardware_allow and r.get("hardware_bucket") not in hardware_allow:
            continue
        if rx and (not r.get("repo_full_name") or not rx.search(r["repo_full_name"])):
            continue
        if args.strict:
            if not r.get("repo_full_name") or not r.get("commit_sha"):
                invalid.append(r)
                continue
        filtered.append(r)

    if args.strict and invalid:
        print(f"[ERROR] strict 模式下发现 {len(invalid)} 条无效记录（repo_full_name/commit_sha 缺失）", file=sys.stderr)
        for r in invalid[:10]:
            print(
                f"  row_index={r.get('row_index')} repo_url={r.get('repo_url')} commit_sha={r.get('commit_sha')}",
                file=sys.stderr,
            )
        return 2

    manifest["repos"] = filtered
    manifest["source"]["repo_count"] = len(filtered)

    save_manifest(manifest, args.out)

    jobs = None
    if args.emit_run_matrix:
        jobs = expand_runs(manifest)
        save_run_matrix(jobs, args.emit_run_matrix)

    print(f"[OK] wrote manifest: {args.out} (repos={manifest['source']['repo_count']})")
    if args.emit_run_matrix:
        print(f"[OK] wrote run_matrix: {args.emit_run_matrix} (jobs={len(jobs)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
