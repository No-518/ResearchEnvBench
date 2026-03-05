#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Host Orchestrator (主程序)

职责：
- 读取 M1 产物 run_matrix.jsonl（或 json / list）
- 对每个 job 调用 M2: m2/run_one_job.py
- 将每个 job 的输出写入 results/<run_id>/jobs/<job_id>/
- 最后调用 M5: m5/build_master_table.py 生成 master_table.csv / master_table.xlsx
- (可选) 调用可视化脚本 viz/plot_master_table.py

设计原则：
- 主程序不信 agent “口头成功”，只信客观产物：
  - /opt/scimlopsbench/report.json 是否存在/可解析（由 M2 校验并落盘到 job dir）
  - repo 内执行 benchmark_scripts/run_all.sh 的 build_output/summary/results.json
- 主程序只负责调度；环境配置(M3)与评估(M4)由 M2 中的 run_one_job 驱动。

依赖：
- Python stdlib（主程序本身不依赖 pandas）
- 运行 job 需要 docker + nvidia runtime（由 M2 负责）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# -----------------------------
# 基础工具
# -----------------------------

def utc_now_compact() -> str:
    return dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write_text(p: Path, s: str) -> None:
    mkdir(p.parent)
    p.write_text(s, encoding="utf-8")


def write_json(p: Path, obj: Any) -> None:
    mkdir(p.parent)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_json_load(p: Path) -> Optional[Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def sha1_short(s: str, n: int = 10) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def slugify(s: str, max_len: int = 120) -> str:
    # 文件夹名安全：只保留 [a-zA-Z0-9._-]，其余转 _
    t = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())
    t = t.strip("._-")
    if not t:
        t = "job"
    if len(t) > max_len:
        t = t[:max_len]
    return t


def parse_owner_repo(repo_url: str) -> Tuple[str, str]:
    """
    从 repo_url 里尽量解析 owner/repo，用于 job_id 命名。
    """
    u = (repo_url or "").strip()
    # 兼容 github.com/owner/repo(.git)
    m = re.search(r"github\.com/([^/]+)/([^/]+)", u)
    if m:
        owner = m.group(1)
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo
    # fallback
    return "unknown", slugify(u, 60) or "repo"


def normalize_hw_bucket(x: str) -> str:
    """Normalize hardware bucket.

    Supported buckets:
      - auto: choose GPUs automatically based on host availability
      - cpu: force CPU-only container
      - single: force 1 GPU (device=0)
      - multi: force all GPUs (>=2)
    """
    s = (x or "").strip().lower()
    if s in ("", "auto", "any", "default"):
        return "auto"
    if s in ("cpu", "cpu/lightgpu", "cpulightgpu", "light", "lightgpu"):
        return "cpu"
    if "multi" in s:
        return "multi"
    if "single" in s:
        return "single"
    # unknown -> auto (safer for minimal-input tables)
    return "auto"


def normalize_baseline(x: str) -> str:
    """
    run_one_job.py 接受的 baseline choices:
      [
        "nexau","nex","nex-n1",
        "nexau_deepseek31_nexn1",
        "nexau_gemini30","nexau_claude_sonnet45","nexau_minimax25",
        "codex","claude_code","claude"
      ]
    这里做一个宽松归一化，尽量兼容 run_matrix 中不同写法。
    """
    b = (x or "").strip().lower()
    b = b.replace("-", "_")
    b = b.replace(".", "")

    if b in ("nex", "nex_n1", "nexau"):
        return "nexau"
    if b in ("nexau_deepseek31_nexn1", "deepseek31", "deepseek_31", "deepseek31_nexn1", "nexn1_deepseek31"):
        return "nexau_deepseek31_nexn1"
    if b in ("nexau_gemini30", "gemini30", "gemini_30"):
        return "nexau_gemini30"
    if b in ("nexau_claude_sonnet45", "nexau_claude_sonnet_45", "claude_sonnet45", "claude_sonnet_45", "sonnet45"):
        return "nexau_claude_sonnet45"
    if b in ("nexau_minimax25", "minimax25", "minimax_m25", "m2_5", "m25"):
        return "nexau_minimax25"
    if b in ("claude", "claude_code", "claudecode"):
        return "claude_code"
    if b == "codex":
        return "codex"
    # fallback：仍返回原值（可能你以后扩展）
    return b


def pick_first(d: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in ("", None):
            return d[k]
    return default


# -----------------------------
# 读取 run_matrix
# -----------------------------

def load_run_matrix(path: Path) -> List[Dict[str, Any]]:
    """
    支持：
    - .jsonl: 每行一个 job dict
    - .json: dict(list) 或 list
    """
    if not path.exists():
        raise FileNotFoundError(f"run_matrix not found: {path}")

    if path.suffix.lower() == ".jsonl":
        jobs: List[Dict[str, Any]] = []
        for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise ValueError(f"Invalid JSONL at line {ln}: {e}")
            if not isinstance(obj, dict):
                raise ValueError(f"Each JSONL line must be an object/dict. line={ln}")
            jobs.append(obj)
        return jobs

    # .json
    data = safe_json_load(path)
    if data is None:
        raise ValueError(f"Invalid JSON: {path}")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        # 常见结构：{"jobs":[...]} / {"run_matrix":[...]}
        for k in ("jobs", "run_matrix", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        raise ValueError(f"Unsupported JSON structure for run_matrix: keys={list(data.keys())}")
    raise ValueError(f"Unsupported run_matrix format: {path}")


# -----------------------------
# Job 运行与恢复策略
# -----------------------------

def job_already_exists(job_dir: Path) -> bool:
    return job_dir.exists() and any(job_dir.iterdir())


def job_has_summary(job_dir: Path) -> bool:
    return (job_dir / "job_summary.json").exists()

def job_is_success(job_dir: Path) -> bool:
    """Best-effort success detection from job_summary.json.

    We treat a job as successful if:
      - report_ok is true, AND
      - runall is "success" (or runall_exit_code == 0 if present).
    """
    p = job_dir / "job_summary.json"
    d = safe_json_load(p)
    if not isinstance(d, dict):
        return False

    status = d.get("status")
    if not isinstance(status, dict):
        return False

    report_ok = status.get("report_ok")
    runall = status.get("runall")
    runall_exit_code = status.get("runall_exit_code")

    ok_report = bool(report_ok) is True
    ok_runall = (runall == "success") or (runall_exit_code == 0)
    return ok_report and ok_runall


def build_job_id(run_id: str, repo_url: str, commit_sha: str, baseline: str, prefix: str = "") -> str:
    owner, repo = parse_owner_repo(repo_url)
    sha_short = (commit_sha or "")[:8] if commit_sha else "nosha"
    base = f"{owner}__{repo}__{sha_short}__{baseline}"
    h = sha1_short(f"{run_id}|{repo_url}|{commit_sha}|{baseline}", 8)
    if prefix:
        base = f"{prefix}__{base}"
    return slugify(f"{base}__{h}", 160)


def run_subprocess(
    cmd: List[str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: Optional[Dict[str, str]] = None,
) -> int:
    mkdir(stdout_path.parent)

    base_env = os.environ.copy()
    base_env["PYTHONUNBUFFERED"] = "1"
    base_env["PYTHONPATH"] = str(cwd) + os.pathsep + base_env.get("PYTHONPATH", "")

    if env:
        base_env.update(env)

    with open(stdout_path, "w", encoding="utf-8") as out_f, open(stderr_path, "w", encoding="utf-8") as err_f:
        p = subprocess.run(cmd, cwd=str(cwd), env=base_env, stdout=out_f, stderr=err_f)
        return int(p.returncode)


# -----------------------------
# 主流程
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="EnvBench Host Orchestrator (主程序)")

    ap.add_argument("--run-matrix", required=True, help="M1 输出 run_matrix.jsonl (或 json)")
    ap.add_argument("--image", required=True, help="Docker image tag, e.g. scimlopsbench:cuda124")

    # 输出目录
    ap.add_argument("--out-root", default="results", help="所有 runs 的根目录 (default: results)")
    ap.add_argument("--run-id", default="", help="本次 run_id；为空则自动生成 UTC 时间戳")
    ap.add_argument("--run-dir", default="", help="直接指定 run_dir（优先级高于 out-root/run-id）")

    # harness / secrets / 网络
    ap.add_argument("--harness-dir", default="", help="harness repo 根目录，用于挂载到容器 /opt/scimlopsbench/harness")
    ap.add_argument("--secrets-env-file", default="", help="env-file，包含 API keys / langfuse keys 等（可为空）")
    ap.add_argument("--network", default="host", choices=["host", "bridge", "none"])

    # 运行策略
    ap.add_argument("--resume", action="store_true", help="如果 job_dir 已存在且包含 job_summary.json，则跳过该 job")
    ap.add_argument(
        "--resume-success-only",
        action="store_true",
        help="配合 --resume 使用：仅当 job_summary.json 显示成功才跳过；失败/超时则重跑（不删除旧目录）。",
    )
    ap.add_argument("--overwrite-existing", action="store_true", help="若 job_dir 已存在则删除重跑（危险）")
    ap.add_argument("--stop-on-error", action="store_true", help="任何 job returncode!=0 则立刻停止")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个 job（0 表示不限制）")
    ap.add_argument("--repo-regex", default="", help="只跑 repo_url 或 repo_full_name 匹配该正则的 job")
    ap.add_argument("--baseline", action="append", default=[], help="只跑指定 baseline（可重复多次）")

    # 透传到 run_one_job.py（M2）
    ap.add_argument("--keep-container", action="store_true")
    ap.add_argument("--skip-agent", action="store_true", help="透传给 M2：跳过 agent，生成 stub report.json（用于无 API 的连通性验证）")
    ap.add_argument("--agent-timeout-sec", type=int, default=3600)
    ap.add_argument("--runall-timeout-sec", type=int, default=7200)
    ap.add_argument(
        "--claude-model",
        default="",
        help="可选：传给 claude_code backend 的模型 ID（映射到容器内 CLAUDE_MODEL）。",
    )
    ap.add_argument(
        "--codex-model",
        default="",
        help="可选：传给 codex backend 的模型 ID（映射到容器内 CODEX_MODEL）。",
    )
    ap.add_argument("--stdout-json-report", default="always", choices=["always", "if_missing", "never"])
    ap.add_argument("--appendix-path-in-harness", default="prompts/task_prompt_appendix.md")
    ap.add_argument("--report-retry", type=int, default=1)

    # M5
    ap.add_argument("--build-master-table", action="store_true", help="跑完后调用 M5 生成总表")
    ap.add_argument("--master-csv", default="", help="总表 CSV 输出路径（默认写到 run_dir/master_table.csv）")
    ap.add_argument("--master-xlsx", default="", help="总表 XLSX 输出路径（默认写到 run_dir/master_table.xlsx）")

    # 可视化（可选）
    ap.add_argument("--plots", action="store_true", help="跑完后尝试生成简单图表（需要 pandas + matplotlib）")

    args = ap.parse_args()

    # 解析 run_id / run_dir
    run_id = args.run_id.strip() or utc_now_compact()

    if args.run_dir.strip():
        run_dir = Path(args.run_dir).expanduser().resolve()
    else:
        run_dir = Path(args.out_root).expanduser().resolve() / run_id

    jobs_root = run_dir / "jobs"
    logs_root = run_dir / "logs"
    mkdir(jobs_root)
    mkdir(logs_root)

    # harness_dir 默认：脚本所在仓库根（假设本文件在仓库根）
    harness_dir = Path(args.harness_dir).expanduser().resolve() if args.harness_dir.strip() else Path.cwd().resolve()

    # run_one_job.py 路径
    run_one_job_py = harness_dir / "m2" / "run_one_job.py"
    if not run_one_job_py.exists():
        # 允许用户从其他目录执行：尝试用 run_dir 上溯查找
        raise FileNotFoundError(f"Cannot find m2/run_one_job.py under harness_dir={harness_dir}")

    # M5 build_master_table.py 路径
    build_master_py = harness_dir / "m5" / "build_master_table.py"
    if not build_master_py.exists():
        # 不强制，但如果用户要 build-master-table 会失败
        pass

    # 读取 run_matrix
    run_matrix_path = Path(args.run_matrix).expanduser().resolve()
    jobs = load_run_matrix(run_matrix_path)

    # 过滤 baseline / repo-regex / limit
    baseline_filters = [normalize_baseline(b) for b in args.baseline] if args.baseline else []
    repo_pat = re.compile(args.repo_regex) if args.repo_regex.strip() else None

    filtered: List[Dict[str, Any]] = []
    for job in jobs:
        repo_url = str(pick_first(job, ["repo_url", "repo", "url", "repo链接"], "")).strip()
        repo_full_name = str(pick_first(job, ["repo_full_name", "full_name", "repo名字", "name"], "")).strip()
        baseline = normalize_baseline(str(pick_first(job, ["baseline", "backend", "baseline_name"], "")).strip())

        if baseline_filters and baseline not in baseline_filters:
            continue
        if repo_pat:
            hay = repo_url or repo_full_name
            if not hay or not repo_pat.search(hay):
                continue

        filtered.append(job)

    if args.limit and args.limit > 0:
        filtered = filtered[: args.limit]

    # 保存 run 级别元信息（便于复现）
    run_meta = {
        "run_id": run_id,
        "utc_started_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "run_matrix_path": str(run_matrix_path),
        "image": args.image,
        "harness_dir": str(harness_dir),
        "secrets_env_file": args.secrets_env_file or "",
        "network": args.network,
        "resume": bool(args.resume),
        "resume_success_only": bool(args.resume_success_only),
        "overwrite_existing": bool(args.overwrite_existing),
        "stop_on_error": bool(args.stop_on_error),
        "skip_agent": bool(args.skip_agent),
        "agent_timeout_sec": int(args.agent_timeout_sec),
        "runall_timeout_sec": int(args.runall_timeout_sec),
        "claude_model": args.claude_model.strip(),
        "codex_model": args.codex_model.strip(),
        "stdout_json_report": args.stdout_json_report,
        "appendix_path_in_harness": args.appendix_path_in_harness,
        "report_retry": int(args.report_retry),
        "job_count_total": len(jobs),
        "job_count_selected": len(filtered),
        "filters": {
            "baseline": baseline_filters,
            "repo_regex": args.repo_regex or "",
            "limit": args.limit,
        },
    }
    write_json(run_dir / "run_metadata.json", run_meta)

    # 复制一份 run_matrix 到 run_dir（溯源）
    try:
        shutil.copy2(str(run_matrix_path), str(run_dir / run_matrix_path.name))
    except Exception:
        pass

    # Snapshot denominator-relevant manifests for reproducibility.
    try:
        categories_src = harness_dir / "scripts_repos_test_categories.csv"
        if categories_src.exists():
            shutil.copy2(str(categories_src), str(run_dir / "scripts_repos_test_categories.snapshot.csv"))
    except Exception:
        pass

    # 执行 jobs
    results: List[Dict[str, Any]] = []

    for idx, job in enumerate(filtered, start=1):
        repo_url = str(pick_first(job, ["repo_url", "repo", "url", "repo链接"], "")).strip()
        commit_sha = str(pick_first(job, ["commit_sha", "sha", "commit", "pinned_sha"], "")).strip()
        baseline = normalize_baseline(str(pick_first(job, ["baseline", "backend", "baseline_name"], "")).strip())
        hw_bucket = normalize_hw_bucket(str(pick_first(job, ["hardware_bucket", "hw_bucket", "bucket"], "auto")))

        if not repo_url or not commit_sha or not baseline:
            results.append({
                "index": idx,
                "status": "skipped_invalid_job",
                "reason": "missing repo_url/commit_sha/baseline",
                "job": job,
            })
            continue

        job_id = str(job.get("job_id", "")).strip() or build_job_id(run_id, repo_url, commit_sha, baseline)
        job_dir = jobs_root / job_id

        # resume / overwrite 策略
        if job_already_exists(job_dir):
            if args.overwrite_existing:
                shutil.rmtree(job_dir)
            elif args.resume:
                # Resume semantics:
                # - if job_summary.json exists: skip as completed
                # - otherwise: re-run in-place (keep partial artifacts as breadcrumbs)
                if job_has_summary(job_dir):
                    if args.resume_success_only and not job_is_success(job_dir):
                        # Re-run failed/timeout jobs in-place (do NOT delete).
                        pass
                    else:
                        results.append({
                            "index": idx,
                            "job_id": job_id,
                            "status": "skipped_resume",
                            "job_dir": str(job_dir),
                        })
                        continue
                else:
                    # No summary yet -> re-run in-place.
                    pass
            else:
                # 默认保护：不覆盖
                results.append({
                    "index": idx,
                    "job_id": job_id,
                    "status": "skipped_exists",
                    "reason": "job_dir already exists (use --resume or --overwrite-existing)",
                    "job_dir": str(job_dir),
                })
                continue

        mkdir(job_dir)

        # 允许 job 级别覆盖 timeouts（可选）
        # 兼容 job["timeout_policy"] = {"agent_timeout_sec":..., "runall_timeout_sec":...}
        tp = job.get("timeout_policy") if isinstance(job.get("timeout_policy"), dict) else {}
        agent_timeout = int(pick_first(tp, ["agent_timeout_sec", "agent_timeout"], args.agent_timeout_sec))
        runall_timeout = int(pick_first(tp, ["runall_timeout_sec", "runall_timeout"], args.runall_timeout_sec))

        # 组装 run_one_job 命令
        cmd = [
            sys.executable,
            "-m", "m2.run_one_job",
            "--image", args.image,
            "--job-id", job_id,
            "--run-id", run_id,
            "--repo-url", repo_url,
            "--commit-sha", commit_sha,
            "--baseline", baseline,
            "--hardware-bucket", hw_bucket,
            "--host-job-dir", str(job_dir),
            "--harness-dir", str(harness_dir),
            "--network", args.network,
            "--agent-timeout-sec", str(agent_timeout),
            "--runall-timeout-sec", str(runall_timeout),
            "--stdout-json-report", args.stdout_json_report,
            "--appendix-path-in-harness", args.appendix_path_in_harness,
            "--report-retry", str(int(args.report_retry)),
        ]

        if args.secrets_env_file.strip():
            cmd += ["--secrets-env-file", args.secrets_env_file.strip()]

        if args.keep_container:
            cmd += ["--keep-container"]

        if args.skip_agent:
            cmd += ["--skip-agent"]

        if args.claude_model.strip():
            cmd += ["--claude-model", args.claude_model.strip()]

        if args.codex_model.strip():
            cmd += ["--codex-model", args.codex_model.strip()]

        # job 日志
        job_log_out = logs_root / f"{idx:04d}_{job_id}.stdout.txt"
        job_log_err = logs_root / f"{idx:04d}_{job_id}.stderr.txt"

        # 记录 job spec（落盘到 job_dir）
        write_json(job_dir / "job_spec.json", {
            "job_id": job_id,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "baseline": baseline,
            "hardware_bucket": hw_bucket,
            "timeout_policy": {
                "agent_timeout_sec": agent_timeout,
                "runall_timeout_sec": runall_timeout,
            },
            "source_job": job,
            "run_one_job_cmd": cmd,
        })

        t0 = time.time()
        rc = run_subprocess(cmd, cwd=harness_dir, stdout_path=job_log_out, stderr_path=job_log_err)
        t1 = time.time()

        results.append({
            "index": idx,
            "job_id": job_id,
            "returncode": rc,
            "wall_time_sec": round(t1 - t0, 3),
            "job_dir": str(job_dir),
        })

        if rc != 0 and args.stop_on_error:
            break

    # run 结束写 summary
    run_summary = {
        "run_id": run_id,
        "utc_finished_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "job_results": results,
    }
    write_json(run_dir / "run_summary.json", run_summary)

    # Optional: export Langfuse token usage for this run (non-fatal)
    try:
        export_py = harness_dir / 'tools' / 'langfuse' / 'export_run_tokens.py'
        if export_py.exists():
            cmd_lf = [sys.executable, str(export_py), '--run-dir', str(run_dir)]
            if args.secrets_env_file:
                cmd_lf += ['--secrets-env-file', str(Path(args.secrets_env_file).expanduser())]

            lf_out = logs_root / 'langfuse_export.stdout.txt'
            lf_err = logs_root / 'langfuse_export.stderr.txt'
            _ = run_subprocess(cmd_lf, cwd=harness_dir, stdout_path=lf_out, stderr_path=lf_err)
        else:
            # Missing script is not fatal
            pass
    except Exception as e:
        print(f'[WARN] Langfuse token export failed: {e}', file=sys.stderr)

    # M5: build master table
    if args.build_master_table:
        if not build_master_py.exists():
            print(f"[WARN] m5/build_master_table.py not found under harness_dir={harness_dir}, skip build-master-table", file=sys.stderr)
        else:
            out_csv = Path(args.master_csv).expanduser().resolve() if args.master_csv.strip() else (run_dir / "master_table.csv")
            out_xlsx = Path(args.master_xlsx).expanduser().resolve() if args.master_xlsx.strip() else (run_dir / "master_table.xlsx")

            cmd_m5 = [
                 sys.executable,
                 "-m", "m5.build_master_table",
                 "--results-root", str(jobs_root),
                 "--out-dir", str(run_dir),
             ]
            m5_out = logs_root / "m5_build_master_table.stdout.txt"
            m5_err = logs_root / "m5_build_master_table.stderr.txt"
            rc_m5 = run_subprocess(cmd_m5, cwd=harness_dir, stdout_path=m5_out, stderr_path=m5_err)

            # M5 outputs (written into --out-dir)
            generated_csv = run_dir / "master_table.csv"
            generated_summary_csv = run_dir / "master_summary.csv"
            generated_xlsx = run_dir / "master_table.xlsx"

            # If user provided --master-csv/--master-xlsx, copy outputs there for convenience
            if rc_m5 == 0:
                try:
                    if out_csv != generated_csv and generated_csv.exists():
                        mkdir(out_csv.parent)
                        shutil.copyfile(generated_csv, out_csv)
                    if out_xlsx != generated_xlsx and generated_xlsx.exists():
                        mkdir(out_xlsx.parent)
                        shutil.copyfile(generated_xlsx, out_xlsx)
                except Exception as e:
                    print(f"[WARN] Failed to copy master outputs: {e}", file=sys.stderr)

            write_json(
                run_dir / "m5_status.json",
                {
                    "returncode": rc_m5,
                    "cmd": cmd_m5,
                    "generated_master_csv": str(generated_csv),
                    "generated_master_summary_csv": str(generated_summary_csv),
                    "generated_master_xlsx": str(generated_xlsx),
                    "final_master_csv": str(out_csv),
                    "final_master_xlsx": str(out_xlsx),
                },
            )

            if rc_m5 != 0:
                print(f"[WARN] M5 build_master_table failed with rc={rc_m5}. Check {m5_err}", file=sys.stderr)

            # 可选可视化
            if args.plots:
                viz_py = harness_dir / "viz" / "plot_master_table.py"
                if viz_py.exists():
                    viz_dir = run_dir / "viz"
                    mkdir(viz_dir)
                    cmd_viz = [
                        sys.executable,
                        str(viz_py),
                        "--master-csv", str(out_csv),
                        "--out-dir", str(viz_dir),
                    ]
                    viz_out = logs_root / "viz.stdout.txt"
                    viz_err = logs_root / "viz.stderr.txt"
                    rc_viz = run_subprocess(cmd_viz, cwd=harness_dir, stdout_path=viz_out, stderr_path=viz_err)
                    write_json(run_dir / "viz_status.json", {"returncode": rc_viz, "cmd": cmd_viz, "out_dir": str(viz_dir)})
                else:
                    print("[WARN] --plots enabled but viz/plot_master_table.py not found. Skipped.", file=sys.stderr)

    # 主程序整体 return code：只要有 job returncode!=0 就返回 2（但不阻止跑完全轮）
    any_fail = any(isinstance(r, dict) and r.get("returncode", 0) not in (0, None) for r in results)
    return 2 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
