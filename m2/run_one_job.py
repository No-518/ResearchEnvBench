import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import time
from typing import Dict, Any, Optional, List

import re

from m2.m2_docker import DockerClient, DockerRunSpec, sanitize_docker_inspect

# Report contract: the env-setup agent must write a verifiable environment summary.
# (Entrypoints are fixed by our benchmark scripts; the agent must NOT guess paths/commands.)
REPORT_REQUIRED_KEYS: List[str] = [
    "python_path",
    "python_version",
    "torch_version",
    "cuda_available",
    "gpu_count",
    "ddp_expected_ok",
    "env_tool",
    "env_name",
    "notes",
]
REPORT_REQUIRED_KEYS_CSV = ",".join(REPORT_REQUIRED_KEYS)

STAGES_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]

def _utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _normalize_yes(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("yes", "y", "true", "1")

def _load_categories_rows(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k: (v or "").strip() for k, v in row.items()})
    return rows

def _find_categories_row(rows: List[Dict[str, str]], script_id: str, repo_url: str) -> Optional[Dict[str, str]]:
    if not rows:
        return None
    repo_full = script_id.replace("@", "/")
    url = (repo_url or "").strip()
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")
    for row in rows:
        if (row.get("repo_slug") or "") == script_id:
            return row
    for row in rows:
        if (row.get("repo") or "") == repo_full:
            return row
    for row in rows:
        if (row.get("git_link") or "").rstrip("/") == url:
            return row
    return None

def _skip_map_from_row(row: Optional[Dict[str, str]]) -> Dict[str, str]:
    skip: Dict[str, str] = {}
    if not row:
        return skip
    if not _normalize_yes(row.get("supports_cpu")):
        skip["cpu"] = row.get("cpu_skip_reason") or "repo_not_supported"
    if not _normalize_yes(row.get("supports_single_gpu")):
        skip["single_gpu"] = row.get("single_gpu_skip_reason") or "repo_not_supported"
    if not _normalize_yes(row.get("supports_multi_gpu")):
        skip["multi_gpu"] = row.get("multi_gpu_skip_reason") or "repo_not_supported"
    return skip

def _stage_task(stage: str) -> str:
    return {
        "pyright": "lint",
        "prepare": "prepare",
        "cpu": "train",
        "cuda": "check",
        "single_gpu": "train",
        "multi_gpu": "train",
        "env_size": "measure",
        "hallucination": "validate",
    }.get(stage, "unknown")

def _write_fallback_stage_result(
    stage_dir: str,
    stage: str,
    status: str,
    skip_reason: str,
    failure_category: str,
    decision_reason: str,
    overwrite: bool = False,
) -> None:
    results_path = os.path.join(stage_dir, "results.json")
    payload: Dict[str, Any] = {}
    if os.path.exists(results_path):
        if not overwrite:
            return
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                payload = existing
        except Exception:
            payload = {}
    _mkdir(stage_dir)
    log_path = os.path.join(stage_dir, "log.txt")
    if status == "skipped":
        log_msg = f"[{_utc_now_iso()}] skipped: {skip_reason}\n"
        error_excerpt = ""
        exit_code = 0
    else:
        log_msg = f"[{_utc_now_iso()}] failed: {failure_category}\n"
        error_excerpt = log_msg.strip()
        exit_code = 1
    _write_text(log_path, log_msg)
    base_assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    if not payload:
        payload = {
            "stage": stage,
            "task": _stage_task(stage),
            "command": "",
            "timeout_sec": 0,
            "framework": "unknown",
            "assets": base_assets,
            "meta": {
                "timestamp_utc": _utc_now_iso(),
                "decision_reason": decision_reason,
            },
        }
    else:
        payload.setdefault("stage", stage)
        payload.setdefault("task", _stage_task(stage))
        if payload.get("command") is None:
            payload["command"] = ""
        if payload.get("timeout_sec") in (None, ""):
            payload["timeout_sec"] = 0
        if not payload.get("framework"):
            payload["framework"] = "unknown"
        if not isinstance(payload.get("assets"), dict):
            payload["assets"] = base_assets
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("timestamp_utc", _utc_now_iso())
        meta["decision_reason"] = decision_reason
        payload["meta"] = meta
    payload.update(
        {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
    )
    _write_json(results_path, payload)

def _summarize_stages(build_output_dir: str, decision_reason: str, overwrite: bool = False) -> bool:
    summary_dir = os.path.join(build_output_dir, "summary")
    results_path = os.path.join(summary_dir, "results.json")
    if os.path.exists(results_path) and not overwrite:
        return False
    _mkdir(summary_dir)
    log_path = os.path.join(summary_dir, "log.txt")
    stages_summary: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []
    log_lines: List[str] = [f"[{_utc_now_iso()}] fallback summary for {build_output_dir}"]

    for stage in STAGES_ORDER:
        stage_dir = os.path.join(build_output_dir, stage)
        stage_results_path = os.path.join(stage_dir, "results.json")
        stage_log_path = os.path.join(stage_dir, "log.txt")
        status = "failure"
        exit_code = 1
        failure_category = "missing_stage_results"
        command = ""
        try:
            if os.path.exists(stage_results_path):
                with open(stage_results_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    status = str(data.get("status") or "failure")
                    try:
                        exit_code = int(data.get("exit_code") or 0)
                    except Exception:
                        exit_code = 1
                    failure_category = str(data.get("failure_category") or "")
                    command = str(data.get("command") or "")
                else:
                    failure_category = "invalid_json"
            else:
                failure_category = "missing_stage_results"
        except Exception:
            failure_category = "invalid_json"

        stages_summary[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": stage_results_path,
            "log_path": stage_log_path,
        }

        if status == "skipped":
            skipped_stages.append(stage)
            log_lines.append(f"[{_utc_now_iso()}] {stage}: skipped")
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)
            log_lines.append(f"[{_utc_now_iso()}] {stage}: failure ({failure_category or 'unknown'})")
        else:
            log_lines.append(f"[{_utc_now_iso()}] {stage}: success")

    overall_status = "failure" if failed_stages else "success"
    exit_code = 1 if failed_stages else 0

    summary_payload = {
        "status": overall_status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "summary",
        "task": "summarize",
        "command": "",
        "timeout_sec": 0,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "failure_category": "" if exit_code == 0 else "overall_failure",
        "error_excerpt": "\n".join(log_lines[-50:]) if exit_code != 0 else "",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": {"pyright": {}, "env_size": {}, "hallucination": {}},
        "meta": {
            "timestamp_utc": _utc_now_iso(),
            "decision_reason": decision_reason,
        },
    }
    _write_text(log_path, "\n".join(log_lines) + "\n")
    _write_json(results_path, summary_payload)
    return True

def _read_text_if_exists(path: str) -> str:
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""

def _agent_timeout_detected(agent_dir: str) -> bool:
    for name in ("agent_driver.stderr.txt", "agent_driver.stdout.txt", "agent_log.txt"):
        text = _read_text_if_exists(os.path.join(agent_dir, name))
        t = text.lower()
        if "timeoutexpir" in t or "timed out" in t:
            return True
    return False

def _apply_categories_skip_overrides(
    host_job_dir: str,
    script_id: str,
    repo_url: str,
    harness_dir: str,
    decision_reason: str,
    only_if_failure_category: Optional[str] = None,
) -> Dict[str, Any]:
    bench_dir = os.path.join(host_job_dir, "benchmark")
    build_output_dir = os.path.join(bench_dir, "build_output")
    if not os.path.isdir(build_output_dir):
        return {"skip_map": {}, "overrides_applied": [], "summary_written": False}

    categories_path = os.path.join(harness_dir, "scripts_repos_test_categories.csv")
    skip_map: Dict[str, str] = {}
    if os.path.isfile(categories_path):
        rows = _load_categories_rows(categories_path)
        row = _find_categories_row(rows, script_id, repo_url)
        skip_map = _skip_map_from_row(row)

    if not skip_map:
        return {"skip_map": {}, "overrides_applied": [], "summary_written": False}

    overrides_applied: List[str] = []
    for stage, skip_reason in skip_map.items():
        stage_dir = os.path.join(build_output_dir, stage)
        results_path = os.path.join(stage_dir, "results.json")
        if not os.path.exists(results_path):
            _write_fallback_stage_result(
                stage_dir,
                stage,
                "skipped",
                skip_reason or "repo_not_supported",
                "not_applicable",
                decision_reason,
                overwrite=False,
            )
            overrides_applied.append(stage)
            continue

        existing: Dict[str, Any] = {}
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                existing = data
        except Exception:
            existing = {}

        if not existing:
            _write_fallback_stage_result(
                stage_dir,
                stage,
                "skipped",
                skip_reason or "repo_not_supported",
                "not_applicable",
                decision_reason,
                overwrite=True,
            )
            overrides_applied.append(stage)
            continue

        status = str(existing.get("status") or "")
        if status == "skipped":
            continue
        failure_category = str(existing.get("failure_category") or "")
        if only_if_failure_category and failure_category != only_if_failure_category:
            continue

        _write_fallback_stage_result(
            stage_dir,
            stage,
            "skipped",
            skip_reason or "repo_not_supported",
            "not_applicable",
            decision_reason,
            overwrite=True,
        )
        overrides_applied.append(stage)

    summary_written = False
    if overrides_applied:
        summary_written = _summarize_stages(build_output_dir, decision_reason, overwrite=True)

    return {
        "skip_map": skip_map,
        "overrides_applied": overrides_applied,
        "summary_written": summary_written,
        "decision_reason": decision_reason,
    }

def _write_timeout_fallback_outputs(
    host_job_dir: str,
    script_id: str,
    repo_url: str,
    commit_sha: str,
    harness_dir: str,
    reason: str,
    overwrite_existing: bool = False,
    decision_prefix: str = "m2 timeout fallback",
) -> Dict[str, Any]:
    bench_dir = os.path.join(host_job_dir, "benchmark")
    build_output_dir = os.path.join(bench_dir, "build_output")
    _mkdir(build_output_dir)

    categories_path = os.path.join(harness_dir, "scripts_repos_test_categories.csv")
    skip_map: Dict[str, str] = {}
    if os.path.isfile(categories_path):
        rows = _load_categories_rows(categories_path)
        row = _find_categories_row(rows, script_id, repo_url)
        skip_map = _skip_map_from_row(row)

    decision_reason = f"{decision_prefix}: {reason}"
    stage_results_written: List[str] = []
    for stage in STAGES_ORDER:
        stage_dir = os.path.join(build_output_dir, stage)
        results_path = os.path.join(stage_dir, "results.json")
        will_write = overwrite_existing or not os.path.exists(results_path)
        if stage in skip_map:
            _write_fallback_stage_result(
                stage_dir,
                stage,
                "skipped",
                skip_map.get(stage, "repo_not_supported"),
                "not_applicable",
                decision_reason,
                overwrite=overwrite_existing,
            )
            if will_write:
                stage_results_written.append(stage)
            continue
        _write_fallback_stage_result(
            stage_dir,
            stage,
            "failure",
            "not_applicable",
            "timeout",
            decision_reason,
            overwrite=overwrite_existing,
        )
        if will_write:
            stage_results_written.append(stage)

    summary_written = _summarize_stages(build_output_dir, decision_reason, overwrite=overwrite_existing)

    if not os.path.exists(os.path.join(bench_dir, "run_all.stderr.txt")):
        _write_text(os.path.join(bench_dir, "run_all.stderr.txt"), decision_reason + "\n")
    if not os.path.exists(os.path.join(bench_dir, "run_all.stdout.txt")):
        _write_text(os.path.join(bench_dir, "run_all.stdout.txt"), "")

    return {
        "skip_map": skip_map,
        "stage_results_written": stage_results_written,
        "summary_written": summary_written,
        "decision_reason": decision_reason,
        "commit_sha": commit_sha,
    }

def repo_url_to_script_id(repo_url: str) -> str:
    """Map repo_url -> scripts bundle directory name.

    Our harness keeps per-repo benchmark scripts under:
      harness/scripts/<owner>@<repo>/benchmark_scripts/
    """
    s = (repo_url or "").strip()
    if s.endswith(".git"):
        s = s[:-4]

    # https://github.com/owner/repo or git@github.com:owner/repo
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)$", s)
    if m:
        return f"{m.group('owner')}@{m.group('repo')}"

    # allow plain owner/repo
    m = re.match(r"^(?P<owner>[^/]+)/(?P<repo>[^/]+)$", s)
    if m:
        return f"{m.group('owner')}@{m.group('repo')}"

    # fall back to a sanitized string (should not happen for our official run matrix)
    return s.replace("/", "@").replace(":", "@")

def _mkdir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _write_json(path: str, obj: Any) -> None:
    _mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def _write_text(path: str, s: str) -> None:
    _mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)

def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"

def detect_host_gpu_count() -> int:
    """Best-effort detect NVIDIA GPU count on the host (outside container)."""
    try:
        p = subprocess.run(
            ["nvidia-smi", "-L"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode != 0:
            return 0
        # Lines like: "GPU 0: NVIDIA ... (UUID: ...)"
        lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip().lower().startswith("gpu ")]
        return len(lines)
    except Exception:
        return 0

def pick_gpus(hardware_bucket: str) -> Optional[str]:
    """Decide docker --gpus value.

    Policy:
    - cpu: None
    - single: device=0
    - multi: all
    - auto: if host has 0 GPU -> None; 1 GPU -> device=0; >=2 -> all
    """
    hb = (hardware_bucket or "").strip().lower()
    if hb in ("", "auto"):
        n = detect_host_gpu_count()
        if n <= 0:
            return None
        if n == 1:
            return "device=0"
        return "all"
    if hb == "cpu":
        return None
    if hb == "single":
        return "device=0"
    if hb == "multi":
        return "all"
    # unknown -> auto
    n = detect_host_gpu_count()
    if n <= 0:
        return None
    if n == 1:
        return "device=0"
    return "all"

def normalize_backend(baseline: str) -> str:
    '''
    Align M2 baseline names with runners.json backend keys.
    runners.json keys:
      - nexau
      - nexau_deepseek31_nexn1
      - nexau_gemini30
      - nexau_claude_sonnet45
      - nexau_minimax25
      - codex
      - claude_code
    '''
    b = baseline.strip().lower()
    b = b.replace("-", "_").replace(".", "")

    nexau_aliases = {
        "nexau": "nexau",
        "nex": "nexau",
        "nex_n1": "nexau",
        "nexn1": "nexau",
        "nexau_deepseek31_nexn1": "nexau_deepseek31_nexn1",
        "deepseek31": "nexau_deepseek31_nexn1",
        "deepseek_31": "nexau_deepseek31_nexn1",
        "deepseek31_nexn1": "nexau_deepseek31_nexn1",
        "nexn1_deepseek31": "nexau_deepseek31_nexn1",
        "nexau_gemini30": "nexau_gemini30",
        "gemini_30": "nexau_gemini30",
        "gemini30": "nexau_gemini30",
        "nexau_claude_sonnet45": "nexau_claude_sonnet45",
        "nexau_claude_sonnet_45": "nexau_claude_sonnet45",
        "claude_sonnet45": "nexau_claude_sonnet45",
        "claude_sonnet_45": "nexau_claude_sonnet45",
        "sonnet45": "nexau_claude_sonnet45",
        "nexau_minimax25": "nexau_minimax25",
        "minimax_m25": "nexau_minimax25",
        "minimax25": "nexau_minimax25",
        "m25": "nexau_minimax25",
        "m2_5": "nexau_minimax25",
    }
    if b in nexau_aliases:
        return nexau_aliases[b]

    if b in ("nex", "nex-n1", "nexau"):
        return "nexau"
    if b in ("claude", "claude_code"):
        return "claude_code"
    if b == "codex":
        return "codex"
    return b

def default_stdout_json_mode(backend: str, user_mode: str) -> str:
    '''
    For nexau: agent writes file -> stdout-json-report should be never
    For codex/claude_code: runner can force JSON -> default always
    '''
    mode = user_mode.lower().strip()
    if mode not in ("always", "if_missing", "never"):
        mode = "always"
    if backend == "nexau" or backend.startswith("nexau_"):
        return "never"
    return mode

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--repo-url", required=True)
    ap.add_argument("--commit-sha", required=True)

    ap.add_argument(
        "--baseline",
        required=True,
        help=(
            "baseline backend name. Known aliases are normalized "
            "(e.g. nex, claude -> canonical names). "
            "For custom backends, ensure tools/env_setup_runner/runners.json "
            "contains a matching key."
        ),
    )

    ap.add_argument("--hardware-bucket", default="auto", choices=["auto", "cpu", "single", "multi"])
    ap.add_argument("--host-job-dir", required=True, help="host dir to store outputs; mounted to /data/results")
    ap.add_argument("--harness-dir", required=True, help="host harness root, mounted ro to /opt/scimlopsbench/harness")
    ap.add_argument("--secrets-env-file", default=None, help="env-file for API keys (not saved to inspect.json)")
    ap.add_argument("--network", default="host", choices=["host", "bridge", "none"])
    ap.add_argument("--keep-container", action="store_true")
    ap.add_argument("--agent-timeout-sec", type=int, default=3600)
    ap.add_argument("--runall-timeout-sec", type=int, default=7200)

    ap.add_argument(
        "--stdout-json-report",
        default="always",
        choices=["always", "if_missing", "never"],
        help="Whether to force report.json from runner stdout JSON (codex/claude).",
    )

    ap.add_argument("--appendix-path-in-harness", default="prompts/task_prompt_appendix.md")
    ap.add_argument("--report-retry", type=int, default=1)
    ap.add_argument(
        "--claude-model",
        default="",
        help="Optional Claude model id for claude_code backend (passed via CLAUDE_MODEL).",
    )
    ap.add_argument(
        "--codex-model",
        default="",
        help="Optional Codex model id for codex backend (passed via CODEX_MODEL).",
    )

    # ✅ skip agent（用于没 API 的情况下先验证 M2+M4 管线）
    ap.add_argument(
        "--skip-agent",
        action="store_true",
        help="Skip running env-setup agent (M3). Will write a stub report.json then proceed to M4.",
    )

    args = ap.parse_args()

    try:
        host_uid = os.getuid()
        host_gid = os.getgid()
    except AttributeError:
        host_uid = None
        host_gid = None

    host_job_dir = os.path.abspath(args.host_job_dir)
    _mkdir(host_job_dir)

    paths = {
        "docker": os.path.join(host_job_dir, "docker"),
        "agent": os.path.join(host_job_dir, "agent"),
        "benchmark": os.path.join(host_job_dir, "benchmark"),
    }
    for p in paths.values():
        _mkdir(p)

    m2_log = os.path.join(paths["docker"], "m2_driver.log")
    dc = DockerClient(log_path=m2_log)

    container_name = f"scimlops_{args.run_id}_{args.job_id}".replace("/", "_").replace(":", "_")
    gpus = pick_gpus(args.hardware_bucket)

    backend = normalize_backend(args.baseline)
    stdout_json_mode = default_stdout_json_mode(backend, args.stdout_json_report)

    script_id = repo_url_to_script_id(args.repo_url)

    run_spec = DockerRunSpec(
        image=args.image,
        name=container_name,
        shm_size="16g",
        network=args.network,
        gpus=gpus,
        env={
            "RUN_ID": args.run_id,
            "JOB_ID": args.job_id,
            "REPO_URL": args.repo_url,
            "COMMIT_SHA": args.commit_sha,
            "BASELINE": backend,
            "SCIMLOPSBENCH_REPORT": "/opt/scimlopsbench/report.json",
            "PYTHONUNBUFFERED": "1",
        },
        env_file=args.secrets_env_file,
        labels={
            "scimlopsbench.run_id": args.run_id,
            "scimlopsbench.job_id": args.job_id,
            "scimlopsbench.baseline": backend,
        },
        volumes=[
            (host_job_dir, "/data/results", "rw"),
            (os.path.abspath(args.harness_dir), "/opt/scimlopsbench/harness", "ro"),
        ],
        workdir="/data/project",
        command="mkdir -p /data/project /data/results /opt/scimlopsbench && sleep infinity",
    )


    claude_config_src: Optional[str] = None
    claude_run_user: Optional[str] = None
    claude_auth_mounted = False

    if backend == "codex":
        host_codex_dir = os.path.expanduser("~/.codex")
        if os.path.isdir(host_codex_dir):
            run_spec.volumes.append((host_codex_dir, "/opt/host_codex_ro", "ro"))

        host_codex_xdg = os.path.expanduser("~/.config/codex")
        if os.path.isdir(host_codex_xdg):
            run_spec.volumes.append((host_codex_xdg, "/opt/host_codex_xdg_ro", "ro"))

        # 保险：显式指定 HOME（不依赖镜像默认值）
        run_spec.env["HOME"] = "/root"
        run_spec.env["XDG_CONFIG_HOME"] = "/root/.config"
        if args.codex_model.strip():
            run_spec.env.setdefault("CODEX_MODEL", args.codex_model.strip())


    if backend == "claude_code":
        claude_run_user = os.environ.get("CLAUDE_RUN_USER", "claude")
        claude_home = f"/home/{claude_run_user}"
        run_spec.env.setdefault("CLAUDE_RUN_USER", claude_run_user)
        if host_uid and host_uid != 0:
            run_spec.env.setdefault("CLAUDE_RUN_UID", str(host_uid))
        if host_gid and host_gid != 0:
            run_spec.env.setdefault("CLAUDE_RUN_GID", str(host_gid))
        run_spec.env.setdefault("HOME", claude_home)
        run_spec.env.setdefault("XDG_CONFIG_HOME", f"{claude_home}/.config")

        claude_auth_dir = os.environ.get("CLAUDE_AUTH_DIR")
        if not claude_auth_dir:
            default_auth_dir = os.path.join(os.path.abspath(args.harness_dir), "secrets", "claude_auth")
            if os.path.isdir(default_auth_dir):
                claude_auth_dir = default_auth_dir

        if claude_auth_dir:
            claude_auth_dir = os.path.abspath(os.path.expanduser(claude_auth_dir))
            if os.path.isdir(claude_auth_dir):
                run_spec.volumes.append((claude_auth_dir, "/opt/claude_config", "rw"))
                run_spec.env.setdefault("CLAUDE_CONFIG_DIR", "/opt/claude_config")
                run_spec.env.setdefault("HOME", claude_home)
                run_spec.env.setdefault("XDG_CONFIG_HOME", f"{claude_home}/.config")
                run_spec.env.setdefault("CLAUDE_AUTH_MOUNTED", "1")
                claude_auth_mounted = True

        if not claude_auth_mounted:
            host_claude_cfg = os.path.expanduser("~/.claude.json")
            if os.path.isfile(host_claude_cfg):
                claude_config_src = host_claude_cfg
                run_spec.env.setdefault("CLAUDE_CONFIG_DIR", "/opt/claude_config")
                run_spec.env.setdefault("HOME", claude_home)
                run_spec.env.setdefault("XDG_CONFIG_HOME", f"{claude_home}/.config")

        if args.network == "host":
            proxy_http = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
            proxy_https = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            proxy_all = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
            proxy_no = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")

            if proxy_http:
                run_spec.env.setdefault("HTTP_PROXY", proxy_http)
                run_spec.env.setdefault("http_proxy", proxy_http)
            if proxy_https:
                run_spec.env.setdefault("HTTPS_PROXY", proxy_https)
                run_spec.env.setdefault("https_proxy", proxy_https)
            if proxy_all:
                run_spec.env.setdefault("ALL_PROXY", proxy_all)
                run_spec.env.setdefault("all_proxy", proxy_all)
            if proxy_no:
                run_spec.env.setdefault("NO_PROXY", proxy_no)
                run_spec.env.setdefault("no_proxy", proxy_no)
        if args.claude_model.strip():
            run_spec.env.setdefault("CLAUDE_MODEL", args.claude_model.strip())


    # IMPORTANT: M5 expects top-level "baseline"
    summary: Dict[str, Any] = {
        "run_id": args.run_id,
        "job_id": args.job_id,
        "repo_url": args.repo_url,
        "commit_sha": args.commit_sha,
        "baseline": backend,
        "baseline_input": args.baseline,
        "baseline_backend": backend,
        "hardware_bucket": args.hardware_bucket,
        "script_id": script_id,
        "gpus": gpus,
        "skip_agent": bool(args.skip_agent),
        "timestamps": {},
        "status": {},
        "container": {},
    }
    if backend == "codex" and args.codex_model.strip():
        summary["status"]["codex_model"] = args.codex_model.strip()
    if backend == "claude_code" and args.claude_model.strip():
        summary["status"]["claude_model"] = args.claude_model.strip()

    cid = ""
    t0 = time.time()
    try:
        summary["timestamps"]["docker_run_start"] = t0
        if backend == "claude_code" and not run_spec.env_file:
            default_env_file = os.path.join(os.path.abspath(args.harness_dir), ".env")
            if os.path.isfile(default_env_file):
                run_spec.env_file = default_env_file
                summary["status"]["secrets_env_file_defaulted"] = default_env_file
        # Best-effort safety for reruns/resume: remove an existing container name.
        # (docker run fails if --name already exists, e.g., after an interrupted run)
        dc.docker(["rm", "-f", container_name], check=False)
        cid = dc.run_detached(run_spec)
        summary["timestamps"]["docker_run_end"] = time.time()
        summary["container"]["id"] = cid
        _write_text(os.path.join(paths["docker"], "container_id.txt"), cid)

        # --- Step 0: record container meta ---
        try:
            img_id = dc.image_id(args.image)
            summary["container"]["image_id"] = img_id
            _write_text(os.path.join(paths["docker"], "image_id.txt"), img_id)
        except Exception as e:
            summary["container"]["image_id_error"] = str(e)

        insp = dc.inspect(cid)
        _write_json(os.path.join(paths["docker"], "inspect.sanitized.json"), sanitize_docker_inspect(insp))

        # GPU visibility diagnostics (best-effort)
        if gpus:
            rc, out, err = dc.exec_bash(
                cid,
                "set -e; echo '[gpu] /dev/nvidia*:'; ls -l /dev/nvidia* 2>/dev/null || true; "
                "echo '[gpu] nvidia-smi -L:'; nvidia-smi -L 2>/dev/null || true; "
                "echo '[gpu] nvidia-smi:'; nvidia-smi 2>/dev/null || true",
                check=False,
            )
            _write_text(os.path.join(paths["docker"], "nvidia_smi.txt"), out + ("\n" + err if err else ""))

        rc, out, err = dc.exec_bash(
            cid,
            "uname -a; lsb_release -a || true; python3 -V || true; which python3 || true; which python || true",
            check=False,
        )
        _write_text(os.path.join(paths["docker"], "system.txt"), out + ("\n" + err if err else ""))

        if backend == "claude_code" and claude_run_user:
            setup_user_cmd = """
            set -euo pipefail
            RUN_USER="${CLAUDE_RUN_USER:-claude}"
            RUN_UID="${CLAUDE_RUN_UID:-}"
            RUN_GID="${CLAUDE_RUN_GID:-}"
            RUN_GROUP="${RUN_USER}"
            if [ -n "${RUN_GID}" ]; then
              if getent group "${RUN_GID}" >/dev/null 2>&1; then
                RUN_GROUP="$(getent group "${RUN_GID}" | cut -d: -f1)"
              else
                groupadd -g "${RUN_GID}" "${RUN_GROUP}"
              fi
            else
              if ! getent group "${RUN_GROUP}" >/dev/null 2>&1; then
                groupadd "${RUN_GROUP}"
              fi
            fi
            if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
              if [ -n "${RUN_UID}" ]; then
                useradd -m -u "${RUN_UID}" -g "${RUN_GROUP}" -s /bin/bash "${RUN_USER}"
              else
                useradd -m -g "${RUN_GROUP}" -s /bin/bash "${RUN_USER}"
              fi
            fi
            mkdir -p /opt/claude_config
            if [ "${CLAUDE_AUTH_MOUNTED:-0}" != "1" ]; then
              chown -R "${RUN_USER}:${RUN_GROUP}" /opt/claude_config
            fi
            """
            dc.exec_bash(cid, setup_user_cmd, check=False)

        if backend == "claude_code" and claude_config_src:
            try:
                dc.exec_bash(cid, "mkdir -p /opt/claude_config", check=False)
                dc.cp_to(cid, claude_config_src, "/opt/claude_config/.claude.json")
                if claude_run_user:
                    dc.exec_bash(
                        cid,
                        f"chown -R {sh_quote(claude_run_user)} /opt/claude_config",
                        check=False,
                    )
                summary["status"]["claude_config_copied"] = True
            except Exception as e:
                summary["status"]["claude_config_copied"] = False
                summary["status"]["claude_config_copy_error"] = str(e)

        # --- Step 1: clone + checkout ---
        summary["timestamps"]["clone_start"] = time.time()
        dc.exec_bash(
            cid,
            f"""
            set -e
            cd /data/project
            rm -rf repo
            git clone {sh_quote(args.repo_url)} repo
            cd repo
            git checkout {sh_quote(args.commit_sha)}
            # Pre-compute a stable C0 denominator from git-tracked Python files.
            # This avoids C0 drift due to agent-created files or caches under the repo root.
            mkdir -p build_output/pyright
            python3 /opt/scimlopsbench/harness/tools/compute_baseline_imports.py \
              --commit {sh_quote(args.commit_sha)} \
              --out build_output/pyright/baseline_imports.json \
              || echo "[m2] WARN: compute_baseline_imports failed (C0 denominator may drift)" >&2
            """,
            timeout_sec=1800,
            check=True,
        )
        summary["timestamps"]["clone_end"] = time.time()
        summary["status"]["clone"] = "success"

        # --- Step 2/3: agent + report ---
        summary["timestamps"]["agent_start"] = time.time()
        agent_timed_out = False

        if args.skip_agent:
            # ✅ 写一个 stub report.json，保证 M4/M5 管线可连通
            stub_cmd = r"""
            set -euo pipefail
            mkdir -p /opt/scimlopsbench
            python3 - <<'PY'
import json, sys
report_path = "/opt/scimlopsbench/report.json"

report = {
  "python_path": sys.executable,
  "python_version": sys.version.split()[0],
  "torch_version": None,
  "cuda_available": None,
  "gpu_count": None,
  "ddp_expected_ok": None,
  "env_tool": "none",
  "env_name": "stub",
  "notes": "agent was skipped (no API keys); stub report.json for pipeline connectivity test"
}

try:
  import torch
  report["torch_version"] = getattr(torch, "__version__", None)
  report["cuda_available"] = bool(torch.cuda.is_available())
  report["gpu_count"] = int(torch.cuda.device_count())
except Exception:
  pass

with open(report_path, "w", encoding="utf-8") as f:
  json.dump(report, f, ensure_ascii=False, indent=2)
print("WROTE", report_path)
PY
            mkdir -p /data/results/agent
            cp -f /opt/scimlopsbench/report.json /data/results/agent/report.json || true
            """
            rc_stub, _, _ = dc.exec_bash(cid, stub_cmd, check=False)
            summary["status"]["agent"] = "skipped"
            summary["status"]["agent_exit_code"] = None
            summary["status"]["stub_report_written"] = (rc_stub == 0)
        else:
            # 正常跑 M3
            agent_cmd = f"""
            set -euo pipefail
            cd /data/project/repo
            mkdir -p /data/results/agent

            HARNESS=/opt/scimlopsbench/harness
            M3DIR="$HARNESS/tools/env_setup_runner"
            PROMPTDIR="$HARNESS/prompts"

            AGENT_PY="$M3DIR/run_env_setup_agent.py"
            RUNNERS_JSON="$M3DIR/runners.json"
            SCHEMA_JSON="$M3DIR/report_schema.json"

            APPENDIX_PATH="$HARNESS/{args.appendix_path_in_harness}"
            APPENDIX_ARG=""
            if [ -f "$APPENDIX_PATH" ]; then
              APPENDIX_ARG="--appendix-prompt $APPENDIX_PATH"
            fi

            python3 "$AGENT_PY" \
              --repo-root /data/project/repo \
              --backend {sh_quote(backend)} \
              --runners-json "$RUNNERS_JSON" \
              --report-schema-path "$SCHEMA_JSON" \
              --system-prompt "$PROMPTDIR/system_prompt.md" \
              --task-prompt "$PROMPTDIR/task_prompt.md" \
              $APPENDIX_ARG \
              --report-path /opt/scimlopsbench/report.json \
              --out-dir /data/results/agent \
              --stdout-json-report {sh_quote(stdout_json_mode)} \
              --timeout-s {int(args.agent_timeout_sec)} \
              --report-retry {int(args.report_retry)} \
              --required-report-keys {sh_quote(REPORT_REQUIRED_KEYS_CSV)} \
              > /data/results/agent/agent_driver.stdout.txt 2> /data/results/agent/agent_driver.stderr.txt
            """
            exec_user = claude_run_user if (backend == "claude_code" and claude_run_user) else None
            rc, _out, _err = dc.exec_bash(
                cid,
                agent_cmd,
                timeout_sec=args.agent_timeout_sec + 300,
                check=False,
                user=exec_user,
            )
            summary["status"]["agent_exit_code"] = rc
            agent_timed_out = (rc != 0) and _agent_timeout_detected(paths["agent"])
            summary["status"]["agent_timed_out"] = agent_timed_out
            if agent_timed_out:
                summary["status"]["agent"] = "timeout"
            else:
                summary["status"]["agent"] = "success" if rc == 0 else "failed"

            # copy report to host job dir (already mounted; just cp inside container)
            dc.exec_bash(
                cid,
                "mkdir -p /data/results/agent && (cp -f /opt/scimlopsbench/report.json /data/results/agent/report.json || true)",
                check=False,
            )

        summary["timestamps"]["agent_end"] = time.time()

        # --- Step 3: validate report.json (file exists + valid JSON + required keys) ---
        rc1, _, _ = dc.exec_bash(cid, "test -s /opt/scimlopsbench/report.json", check=False)

        # Validate required keys presence (values may be null)
        py_check = (
            "python3 -c "
            + sh_quote(
                "import json, os; p='/opt/scimlopsbench/report.json'; "
                "r=json.load(open(p,'r',encoding='utf-8')); "
                f"req={REPORT_REQUIRED_KEYS!r}; "
                "assert isinstance(r, dict), 'report_not_object'; "
                "missing=[k for k in req if k not in r]; "
                "assert not missing, 'missing_keys=' + ','.join(missing); "
                "py=r.get('python_path'); "
                "assert isinstance(py,str) and py.strip(), 'python_path_empty'; "
                "assert py.strip().startswith('/'), 'python_path_not_absolute'; "
                "assert os.path.exists(py) and os.access(py, os.X_OK), 'python_path_not_executable'"
            )
        )
        rc2, _, _ = dc.exec_bash(cid, py_check, check=False)

        report_ok = (rc1 == 0 and rc2 == 0)
        summary["status"]["report_exists_nonempty"] = (rc1 == 0)
        summary["status"]["report_valid_json_and_keys"] = (rc2 == 0)
        summary["status"]["report_required_keys"] = REPORT_REQUIRED_KEYS
        summary["status"]["report_ok"] = report_ok

        if agent_timed_out:
            summary["timestamps"]["runall_start"] = time.time()
            fallback_info = _write_timeout_fallback_outputs(
                host_job_dir=host_job_dir,
                script_id=script_id,
                repo_url=args.repo_url,
                commit_sha=args.commit_sha,
                harness_dir=os.path.abspath(args.harness_dir),
                reason="agent_timeout",
                overwrite_existing=True,
                decision_prefix="agent timeout fallback",
            )
            summary["status"]["runall_exit_code"] = None
            summary["status"]["runall"] = "failed"
            summary["status"]["runall_skipped_due_to_agent_timeout"] = True
            summary["status"]["timeout_fallback"] = fallback_info
            summary["timestamps"]["runall_end"] = time.time()
            summary["timestamps"]["job_end"] = time.time()
            _write_json(os.path.join(host_job_dir, "job_summary.json"), summary)
            return

        # --- Step 4/5: inject benchmark_scripts & run run_all.sh & archive build_output ---
        summary["timestamps"]["runall_start"] = time.time()
        runall_cmd = f"""
        set -euo pipefail
        cd /data/project/repo
        export SCIMLOPSBENCH_REPORT="/opt/scimlopsbench/report.json"

        # Benchmark scripts are FIXED per repo and live in the harness under:
        #   /opt/scimlopsbench/harness/scripts/<owner>@<repo>/benchmark_scripts/
        BUNDLE={sh_quote(script_id)}
        SCRIPTS_ROOT=/opt/scimlopsbench/harness/scripts
        BUNDLE_DIR="$SCRIPTS_ROOT/$BUNDLE/benchmark_scripts"

        if [ ! -d "$BUNDLE_DIR" ]; then
          # fallback: try prefix match (e.g., if a bundle has a suffix)
          match=$(ls -d "$SCRIPTS_ROOT/$BUNDLE"* 2>/dev/null | head -n 1 || true)
          if [ -n "$match" ] && [ -d "$match/benchmark_scripts" ]; then
            BUNDLE_DIR="$match/benchmark_scripts"
          else
            echo "[m2] benchmark_scripts bundle not found for $BUNDLE under $SCRIPTS_ROOT" >&2
            echo "[m2] available bundles:" >&2
            ls -1 "$SCRIPTS_ROOT" >&2 || true
            exit 20
          fi
        fi

        rm -rf benchmark_scripts
        mkdir -p benchmark_scripts
        rsync -a "$BUNDLE_DIR/" ./benchmark_scripts/
        chmod +x benchmark_scripts/run_all.sh || true
        chmod -R a+rx benchmark_scripts || true

        mkdir -p /data/results/benchmark

        # IMPORTANT:
        # - run_all.sh 通常“只要任一 stage 失败就返回非 0”
        # - 但我们依然要把 build_output 归档出来，方便 debug + M5 汇总
        set +e
        bash benchmark_scripts/run_all.sh > /data/results/benchmark/run_all.stdout.txt 2> /data/results/benchmark/run_all.stderr.txt
        RUNALL_RC=$?
        set -e

        # Collect build_output even if run_all failed.
        if [ -d build_output ]; then
          rsync -a build_output/ /data/results/benchmark/build_output/ || true
        else
          found=$(find . -maxdepth 3 -type d -name build_output | head -n 1 || true)
          if [ -n "$found" ]; then
            rsync -a "$found"/ /data/results/benchmark/build_output/ || true
          fi
        fi

        exit $RUNALL_RC

        """
        rc_runall, _, _ = dc.exec_bash(cid, runall_cmd, timeout_sec=args.runall_timeout_sec, check=False)
        summary["timestamps"]["runall_end"] = time.time()
        summary["status"]["runall_exit_code"] = rc_runall

        # Ensure build_output under the host-mounted results dir is writable by the host user.
        # Otherwise, host-side post-processing (skip overrides / fallback summaries) may crash.
        if host_uid and host_gid and host_uid != 0 and host_gid != 0:
            dc.exec_bash(cid, f"chown -R {host_uid}:{host_gid} /data/results || true", check=False)

        skip_override_info = _apply_categories_skip_overrides(
            host_job_dir=host_job_dir,
            script_id=script_id,
            repo_url=args.repo_url,
            harness_dir=os.path.abspath(args.harness_dir),
            decision_reason="categories skip override (post-run)",
        )
        if skip_override_info.get("overrides_applied"):
            summary["status"]["skip_overrides"] = skip_override_info

        # Extract per-stage statuses from the benchmark summary, if present.
        stage_statuses = None
        failed_stages = None
        skipped_stages = None
        summary_results_path = os.path.join(host_job_dir, "benchmark", "build_output", "summary", "results.json")
        try:
            if os.path.exists(summary_results_path):
                with open(summary_results_path, "r", encoding="utf-8") as f:
                    sr = json.load(f)
                if isinstance(sr, dict):
                    stages = sr.get("stages")
                    if isinstance(stages, dict):
                        stage_statuses = {
                            k: (v.get("status") if isinstance(v, dict) else None)
                            for k, v in stages.items()
                        }
                    failed_stages = sr.get("failed_stages") if isinstance(sr.get("failed_stages"), list) else None
                    skipped_stages = sr.get("skipped_stages") if isinstance(sr.get("skipped_stages"), list) else None
        except Exception:
            stage_statuses = None

        summary["status"]["runall_stage_statuses"] = stage_statuses
        summary["status"]["runall_failed_stages"] = failed_stages
        summary["status"]["runall_skipped_stages"] = skipped_stages

        # In our per-repo benchmark scripts, run_all.sh returns non-zero if any stage failed.
        summary["status"]["runall"] = "success" if rc_runall == 0 else "failed"

        summary["timestamps"]["job_end"] = time.time()
        _write_json(os.path.join(host_job_dir, "job_summary.json"), summary)

    except Exception as e:
        summary["timestamps"]["job_end"] = time.time()
        summary["status"]["m2_exception"] = str(e)
        # Best-effort cleanup: make any container-written artifacts deletable by the host user.
        try:
            if cid and host_uid and host_gid and host_uid != 0 and host_gid != 0:
                dc.exec_bash(cid, f"chown -R {host_uid}:{host_gid} /data/results || true", check=False)
        except Exception as ce:
            summary["status"]["chown_results_error"] = str(ce)
        if isinstance(e, subprocess.TimeoutExpired):
            summary["status"]["m2_timeout"] = True
            try:
                fallback_info = _write_timeout_fallback_outputs(
                    host_job_dir=host_job_dir,
                    script_id=script_id,
                    repo_url=args.repo_url,
                    commit_sha=args.commit_sha,
                    harness_dir=os.path.abspath(args.harness_dir),
                    reason=str(e),
                )
                summary["status"]["timeout_fallback"] = fallback_info
                skip_override_info = _apply_categories_skip_overrides(
                    host_job_dir=host_job_dir,
                    script_id=script_id,
                    repo_url=args.repo_url,
                    harness_dir=os.path.abspath(args.harness_dir),
                    decision_reason="categories skip override (post-timeout)",
                )
                if skip_override_info.get("overrides_applied"):
                    summary["status"]["skip_overrides"] = skip_override_info
            except Exception as fe:
                summary["status"]["timeout_fallback_error"] = str(fe)
        _write_json(os.path.join(host_job_dir, "job_summary.json"), summary)
        raise
    finally:
        if cid and not args.keep_container:
            dc.remove(cid, force=True)

if __name__ == "__main__":
    main()
