#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Compute baseline (git-tracked) imported-package set for a repo at a fixed commit.

This is shared evaluation infrastructure:
- We want C0's denominator to be stable across agents/models.
- Some agents create extra files under the repo root, which can change a naive rglob-based scan.

We therefore compute the imported-package set from git-tracked Python files only.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable, Set


EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "build",
    "dist",
    "node_modules",
    "build_output",
    "benchmark_assets",
    "benchmark_scripts",
}


def _run_git(repo_root: Path, args: list[str]) -> str:
    out = subprocess.check_output(["git", *args], cwd=str(repo_root), stderr=subprocess.DEVNULL)
    return out.decode("utf-8", errors="replace")


def _is_excluded(path: PurePosixPath) -> bool:
    return any(part in EXCLUDE_DIRS for part in path.parts)


def iter_tracked_python_files(repo_root: Path, commit: str) -> Iterable[PurePosixPath]:
    out = _run_git(repo_root, ["ls-tree", "-r", "--name-only", commit])
    for line in out.splitlines():
        s = line.strip()
        if not s or not s.endswith(".py"):
            continue
        p = PurePosixPath(s)
        if _is_excluded(p):
            continue
        yield p


def collect_imported_packages_from_source(src: str) -> Set[str]:
    pkgs: Set[str] = set()
    try:
        tree = ast.parse(src)
    except Exception:
        return pkgs

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = (alias.name or "").split(".", 1)[0].strip()
                if name:
                    pkgs.add(name)
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0) == 0 and getattr(node, "module", None):
                name = str(node.module).split(".", 1)[0].strip()
                if name:
                    pkgs.add(name)
    return pkgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", type=Path, default=Path("."), help="Repo root (default: .)")
    ap.add_argument("--commit", default="HEAD", help="Commit SHA (default: HEAD)")
    ap.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = ap.parse_args()

    repo_root: Path = args.repo_root.resolve()
    commit = str(args.commit).strip() or "HEAD"

    tracked_files = list(iter_tracked_python_files(repo_root, commit))
    imported: Set[str] = set()
    files_read = 0
    files_missing_on_disk = 0

    for rel in tracked_files:
        p = repo_root / Path(rel.as_posix())
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            files_missing_on_disk += 1
            continue
        files_read += 1
        imported |= collect_imported_packages_from_source(src)

    payload = {
        "schema_version": 1,
        "commit": commit,
        "repo_root": str(repo_root),
        "files_tracked_py_count": len(tracked_files),
        "files_read_count": files_read,
        "files_missing_on_disk_count": files_missing_on_disk,
        "total_imported_packages_count": len(imported),
        "imported_packages": sorted(imported),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

