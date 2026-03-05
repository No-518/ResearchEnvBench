from __future__ import annotations
from typing import Any, Dict, List, Optional
from pathlib import Path
import datetime as _dt
import hashlib
import json

import pandas as pd

from .xlsx_reader import load_table
from .normalize import (
    parse_repo_full_name, normalize_commit_sha, parse_bool,
    parse_eval_dims, parse_repo_type, parse_hardware_bucket, split_csv
)

def _now_utc_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _stable_job_id(repo_full_name: str, commit_sha: str, baseline: str) -> str:
    raw = f"{repo_full_name}@{commit_sha}::{baseline}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]

def _cell_str(val: Any) -> str:
    if val is None or pd.isna(val):
        return ""
    return str(val)

def build_manifest(
    xlsx_path: str,
    sheet: Optional[str],
    default_baselines: List[str],
    include_unusable: bool = False,
) -> Dict[str, Any]:
    table = load_table(xlsx_path, sheet)
    df = table.df

    repos: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        repo_url_raw = row.get(table.colmap["repo_url"])
        commit_sha_raw = row.get(table.colmap["commit_sha"])

        repo_url = _cell_str(repo_url_raw).strip()
        commit_sha = normalize_commit_sha(None if pd.isna(commit_sha_raw) else commit_sha_raw)

        # skip empty rows
        if not repo_url and commit_sha is None:
            continue

        repo_full_name = parse_repo_full_name(repo_url) if repo_url else None

        usable = None
        if "usable" in table.colmap:
            usable = parse_bool(row.get(table.colmap["usable"]))
        if usable is False and not include_unusable:
            continue

        hardware_bucket, hardware_tags = ("cpu", {"raw": None})
        if "hardware_desc" in table.colmap:
            hardware_bucket, hardware_tags = parse_hardware_bucket(row.get(table.colmap["hardware_desc"]))

        entry: Dict[str, Any] = {
            "row_index": int(idx),
            "repo_url": repo_url or None,
            "repo_full_name": repo_full_name,
            "commit_sha": commit_sha,
            "repo_id": (repo_full_name or f"row{idx}").replace("/", "__") if repo_full_name else f"row{idx}",
            "hardware_bucket": hardware_bucket,
            "hardware_tags": hardware_tags,
            "baseline_targets": list(default_baselines),
            "timeout_policy": {},   # optional overrides will be put here
        }

        # optional fields (best-effort)
        if "repo_name" in table.colmap:
            entry["repo_name"] = _cell_str(row.get(table.colmap["repo_name"])).strip() or None
        if "repo_type" in table.colmap:
            entry["repo_type"] = parse_repo_type(row.get(table.colmap["repo_type"]))
        if "difficulty" in table.colmap:
            entry["difficulty"] = _cell_str(row.get(table.colmap["difficulty"])).strip() or None
        if "eval_dims" in table.colmap:
            entry["eval_dims"] = parse_eval_dims(row.get(table.colmap["eval_dims"]))
        else:
            entry["eval_dims"] = []
        if "notes" in table.colmap:
            entry["notes"] = _cell_str(row.get(table.colmap["notes"])).strip() or None

        # per-repo baseline override (optional column)
        if "baseline_targets" in table.colmap:
            overrides = split_csv(row.get(table.colmap["baseline_targets"]))
            if overrides:
                entry["baseline_targets"] = overrides

        # per-repo timeout overrides (optional columns)
        if "timeout_agent_sec" in table.colmap:
            v = row.get(table.colmap["timeout_agent_sec"])
            if v is not None and not pd.isna(v):
                try:
                    entry["timeout_policy"]["agent_sec"] = int(float(v))
                except Exception:
                    pass
        if "timeout_run_all_sec" in table.colmap:
            v = row.get(table.colmap["timeout_run_all_sec"])
            if v is not None and not pd.isna(v):
                try:
                    entry["timeout_policy"]["run_all_sec"] = int(float(v))
                except Exception:
                    pass

        if "tests_ready" in table.colmap:
            entry["tests_ready"] = parse_bool(row.get(table.colmap["tests_ready"]))
        if "manual_ready" in table.colmap:
            entry["manual_ready"] = parse_bool(row.get(table.colmap["manual_ready"]))
        if "framework" in table.colmap:
            entry["framework"] = _cell_str(row.get(table.colmap["framework"])).strip() or None
        if "task" in table.colmap:
            entry["task"] = _cell_str(row.get(table.colmap["task"])).strip() or None
        if "dataset" in table.colmap:
            entry["dataset"] = _cell_str(row.get(table.colmap["dataset"])).strip() or None
        if "models" in table.colmap:
            entry["models"] = _cell_str(row.get(table.colmap["models"])).strip() or None
        if "paper" in table.colmap:
            entry["paper"] = _cell_str(row.get(table.colmap["paper"])).strip() or None
        if usable is not None:
            entry["usable"] = usable

        # drop empty timeout_policy to keep JSON clean
        if not entry["timeout_policy"]:
            entry.pop("timeout_policy", None)

        repos.append(entry)

    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_utc_iso(),
        "source": {
            "xlsx_path": str(Path(xlsx_path)),
            "sheet": table.sheet,
            "row_count_raw": int(len(df)),
            "repo_count": int(len(repos)),
        },
        "defaults": {
            "baseline_targets": list(default_baselines),
        },
        "repos": repos,
    }
    return manifest

def save_manifest(manifest: Dict[str, Any], out_path: str) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def expand_runs(manifest: Dict[str, Any], baselines: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    for r in manifest.get("repos", []):
        repo_full_name = r.get("repo_full_name")
        commit_sha = r.get("commit_sha")
        if not repo_full_name or not commit_sha:
            continue
        targets = baselines or r.get("baseline_targets") or manifest.get("defaults", {}).get("baseline_targets") or []
        for b in targets:
            jobs.append({
                "job_id": _stable_job_id(repo_full_name, commit_sha, b),
                "repo_id": r.get("repo_id"),
                "repo_full_name": repo_full_name,
                "repo_url": r.get("repo_url"),
                "commit_sha": commit_sha,
                "hardware_bucket": r.get("hardware_bucket", "cpu"),
                "baseline": b,
                "eval_dims": r.get("eval_dims", []),
                "difficulty": r.get("difficulty"),
                "repo_type": r.get("repo_type"),
                "timeout_policy": r.get("timeout_policy", {}),
            })
    return jobs

def save_run_matrix(jobs: List[Dict[str, Any]], out_path: str) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for j in jobs:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
