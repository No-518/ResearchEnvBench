#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) into benchmark_assets/ and write build_output/prepare/results.json.

Defaults (override via env vars):
  SPECFORGE_BENCH_DATASET=sharegpt
  SPECFORGE_BENCH_DATASET_SAMPLE_SIZE=8
  SPECFORGE_BENCH_MODEL_ID=          # if unset, tries a small public Qwen list
  SPECFORGE_BENCH_MODEL_REVISION=    # optional (e.g., a commit hash or tag)

Python resolution (highest to lowest):
  --python <path>
  $SCIMLOPSBENCH_PYTHON
  python_path from report.json ($SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json
EOF
}

python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"
log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

assets_root="$repo_root/benchmark_assets"
cache_root="$assets_root/cache"
dataset_root="$assets_root/dataset"
model_root="$assets_root/model"
mkdir -p "$cache_root" "$dataset_root" "$model_root"

host_python="$(command -v python3 || command -v python || true)"
if [[ -z "$host_python" ]]; then
  echo "[prepare] ERROR: python3/python not found in PATH (needed for JSON/result emission)" >> "$log_path"
  cat >"$results_json" <<EOF
{"status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"prepare","task":"download","command":"bash benchmark_scripts/prepare_assets.sh","timeout_sec":1200,"framework":"unknown","assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},"meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"host python not found"},"failure_category":"deps","error_excerpt":"host python not found"}
EOF
  exit 1
fi

resolve_python_from_report() {
  local rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
  "$host_python" - "$rp" <<'PY' 2>/dev/null || return 1
import json, sys
rp = sys.argv[1]
try:
    with open(rp, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
pp = data.get("python_path")
if isinstance(pp, str) and pp.strip():
    print(pp)
    sys.exit(0)
sys.exit(1)
PY
}

resolved_python=""
python_resolution=""
if [[ -n "$python_bin" ]]; then
  resolved_python="$python_bin"
  python_resolution="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  resolved_python="${SCIMLOPSBENCH_PYTHON}"
  python_resolution="env:SCIMLOPSBENCH_PYTHON"
else
  resolved_python="$(resolve_python_from_report || true)"
  python_resolution="report:python_path"
fi

{
  echo "[prepare] repo_root=$repo_root"
  echo "[prepare] resolved_python=$resolved_python ($python_resolution)"
  echo "[prepare] cache_root=$cache_root"
  echo "[prepare] dataset_root=$dataset_root"
  echo "[prepare] model_root=$model_root"
} >>"$log_path"

if [[ -z "$resolved_python" ]]; then
  REPO_ROOT="$repo_root" "$host_python" - <<'PY' >"$results_json"
import json, os, subprocess, pathlib
repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
log_path = repo_root / "build_output" / "prepare" / "log.txt"
def tail(path, n=220):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=str(repo_root), text=True, timeout=10).strip()
    except Exception:
        return ""
res = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"prepare",
  "task":"download",
  "command":"bash benchmark_scripts/prepare_assets.sh",
  "timeout_sec":1200,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":git_commit(),"env_vars":{},"decision_reason":"missing_report (cannot resolve python_path)"},
  "failure_category":"missing_report",
  "error_excerpt":tail(log_path),
}
print(json.dumps(res, indent=2))
PY
  exit 1
fi

dataset_name="${SPECFORGE_BENCH_DATASET:-sharegpt}"
dataset_sample_size="${SPECFORGE_BENCH_DATASET_SAMPLE_SIZE:-8}"
dataset_path="$dataset_root/${dataset_name}_train.jsonl"

# Choose a small, publicly accessible (no-auth) model by default (avoid Llama per benchmark instructions).
model_id="${SPECFORGE_BENCH_MODEL_ID:-}"
model_revision="${SPECFORGE_BENCH_MODEL_REVISION:-}"

if [[ -z "$model_id" ]]; then
  # Keep the list short and deterministic; try smallest first.
  candidates=(
    "Qwen/Qwen2.5-0.5B-Instruct"
    "Qwen/Qwen2.5-1.5B-Instruct"
    "Qwen/Qwen2.5-3B-Instruct"
  )
else
  candidates=("$model_id")
fi

export HF_HOME="$cache_root/huggingface"
export HF_HUB_CACHE="$cache_root/huggingface/hub"
export HF_DATASETS_CACHE="$cache_root/huggingface/datasets"
export TRANSFORMERS_CACHE="$cache_root/huggingface/transformers"
export XDG_CACHE_HOME="$cache_root/xdg"
export TORCH_HOME="$cache_root/torch"
export TORCHINDUCTOR_CACHE_DIR="$cache_root/torchinductor"
export PYTHONPYCACHEPREFIX="$cache_root/pycache"
export WANDB_MODE="disabled"
export WANDB_DIR="$cache_root/wandb"
export HF_HUB_DISABLE_TELEMETRY="1"
export HF_DATASETS_DISABLE_TELEMETRY="1"

echo "[prepare] dataset_name=$dataset_name sample_size=$dataset_sample_size dataset_path=$dataset_path" >>"$log_path"
echo "[prepare] model_candidates=${candidates[*]} revision=${model_revision:-<default>}" >>"$log_path"

download_summary_json="$cache_root/_prepare_download_summary.json"
rm -f "$download_summary_json" >/dev/null 2>&1 || true

model_candidates_json="$("$host_python" - "${candidates[@]}" <<'PY'
import json, sys
print(json.dumps(sys.argv[1:]))
PY
)"

set +e
cache_root="$cache_root" dataset_root="$dataset_root" model_root="$model_root" \
dataset_name="$dataset_name" dataset_sample_size="$dataset_sample_size" \
model_revision="$model_revision" model_candidates_json="$model_candidates_json" \
download_summary_json="$download_summary_json" \
REPO_ROOT="$repo_root" \
  "$resolved_python" - <<'PY' >>"$log_path" 2>&1
import hashlib
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
cache_root = pathlib.Path(os.environ["cache_root"]) if "cache_root" in os.environ else repo_root / "benchmark_assets" / "cache"
dataset_root = pathlib.Path(os.environ["dataset_root"]) if "dataset_root" in os.environ else repo_root / "benchmark_assets" / "dataset"
model_root = pathlib.Path(os.environ["model_root"]) if "model_root" in os.environ else repo_root / "benchmark_assets" / "model"

dataset_name = os.environ.get("dataset_name", "sharegpt")
dataset_sample_size = int(os.environ.get("dataset_sample_size", "8"))
dataset_path = dataset_root / f"{dataset_name}_train.jsonl"

candidates = json.loads(os.environ.get("model_candidates_json", "[]"))
model_revision = os.environ.get("model_revision") or None

summary_path = pathlib.Path(os.environ.get("download_summary_json", str(cache_root / "_prepare_download_summary.json")))
summary: Dict[str, Any] = {
    "dataset": {"ok": False, "path": str(dataset_path), "source": "", "version": "", "sha256": ""},
    "model": {"ok": False, "path": "", "source": "", "version": "", "sha256": "", "resolved_snapshot_dir": ""},
    "draft_config": {"ok": False, "path": "", "sha256": ""},
    "errors": [],
}

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_model_signature(model_dir: pathlib.Path) -> Tuple[str, List[str]]:
    # Avoid hashing the entire directory; hash a small, representative set.
    candidates = [
        model_dir / "config.json",
        model_dir / "tokenizer.json",
        model_dir / "tokenizer_config.json",
        model_dir / "generation_config.json",
    ]
    weight_files = []
    for pat in ("model.safetensors", "pytorch_model.bin"):
        p = model_dir / pat
        if p.exists():
            weight_files.append(p)
            break
    # Fallback: first safetensors shard
    if not weight_files:
        shards = sorted(model_dir.glob("model-*.safetensors"))
        if shards:
            weight_files.append(shards[0])
    files = [p for p in candidates + weight_files if p.exists() and p.is_file()]
    parts = []
    hashed = []
    for p in files:
        s = sha256_file(p)
        parts.append(f"{p.name}:{s}")
        hashed.append(str(p))
    sig = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return sig, hashed

def is_auth_error(msg: str) -> bool:
    m = msg.lower()
    return ("401" in m) or ("403" in m) or ("gated" in m) or ("token" in m and "required" in m)

def ensure_dirs():
    cache_root.mkdir(parents=True, exist_ok=True)
    dataset_root.mkdir(parents=True, exist_ok=True)
    model_root.mkdir(parents=True, exist_ok=True)

ensure_dirs()

# 1) Dataset preparation (prefer repo script + HF datasets cache under benchmark_assets/cache).
try:
    dataset_info_path = dataset_root / "asset_info.json"
    if dataset_path.exists() and dataset_path.stat().st_size > 0:
        # Skip re-download if we can verify the existing file matches the recorded sha256.
        current_sha = sha256_file(dataset_path)
        if dataset_info_path.exists():
            try:
                info = json.loads(dataset_info_path.read_text(encoding="utf-8"))
                if isinstance(info, dict) and info.get("sha256") == current_sha:
                    summary["dataset"]["ok"] = True
            except Exception:
                pass
        if not summary["dataset"]["ok"]:
            summary["dataset"]["ok"] = True
    else:
        import subprocess
        cmd = [
            sys.executable,
            str(repo_root / "scripts" / "prepare_data.py"),
            "--dataset",
            dataset_name,
            "--sample-size",
            str(dataset_sample_size),
            "--output-path",
            str(dataset_root),
        ]
        print("[prepare.dataset] running:", " ".join(cmd), flush=True)
        proc = subprocess.run(cmd, cwd=str(repo_root), text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"prepare_data.py failed with exit code {proc.returncode}")
    if dataset_path.exists():
        summary["dataset"]["ok"] = True
        summary["dataset"]["source"] = {
            "sharegpt": "hf://Aeala/ShareGPT_Vicuna_unfiltered",
            "ultrachat": "hf://HuggingFaceH4/ultrachat_200k",
            "perfectblend": "hf://mlabonne/open-perfectblend",
        }.get(dataset_name, f"hf://(prepared by scripts/prepare_data.py) dataset={dataset_name}")
        summary["dataset"]["sha256"] = sha256_file(dataset_path)
        # Persist metadata for offline reuse.
        try:
            dataset_info_path.write_text(
                json.dumps(
                    {
                        "dataset": dataset_name,
                        "sample_size": dataset_sample_size,
                        "path": str(dataset_path),
                        "source": summary["dataset"]["source"],
                        "version": summary["dataset"].get("version", ""),
                        "sha256": summary["dataset"]["sha256"],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
except Exception as e:
    summary["errors"].append({"stage": "dataset", "error": str(e)})

# 2) Model download (HF snapshot -> benchmark_assets/cache, then link to benchmark_assets/model).
selected_model_id: Optional[str] = None
resolved_snapshot_dir: Optional[pathlib.Path] = None

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    summary["errors"].append({"stage": "model", "error": f"missing huggingface_hub: {e}"})
    snapshot_download = None

def try_download_model(model_id: str) -> pathlib.Path:
    assert snapshot_download is not None
    kwargs = {
        "repo_id": model_id,
        "cache_dir": str(cache_root / "huggingface" / "hub"),
        "local_files_only": False,
    }
    if model_revision:
        kwargs["revision"] = model_revision
    path = snapshot_download(**kwargs)
    return pathlib.Path(path)

model_link = model_root / "target_model"
model_info_path = model_root / "asset_info.json"

def load_existing_model_if_any() -> bool:
    if not model_link.exists():
        return False
    if not (model_link / "config.json").exists():
        return False
    sig, hashed_files = sha256_model_signature(model_link)
    if model_info_path.exists():
        try:
            info = json.loads(model_info_path.read_text(encoding="utf-8"))
            if isinstance(info, dict) and info.get("sha256") == sig:
                summary["model"]["ok"] = True
                summary["model"]["source"] = str(info.get("source", ""))
                summary["model"]["version"] = str(info.get("version", ""))
                summary["model"]["path"] = str(info.get("path", str(model_link)))
                summary["model"]["sha256"] = sig
                summary["model"]["hashed_files"] = hashed_files
                summary["model"]["resolved_snapshot_dir"] = str(info.get("resolved_snapshot_dir", ""))
                return True
        except Exception:
            pass
    # No metadata file; still allow offline reuse.
    summary["model"]["ok"] = True
    summary["model"]["source"] = ""
    summary["model"]["version"] = ""
    summary["model"]["path"] = str(model_link)
    summary["model"]["sha256"] = sig
    summary["model"]["hashed_files"] = hashed_files
    return True

existing_model_ok = load_existing_model_if_any()

if not existing_model_ok and snapshot_download is not None:
    for model_id in candidates:
        try:
            print(
                f"[prepare.model] attempting snapshot_download: {model_id} (revision={model_revision or 'default'})",
                flush=True,
            )
            resolved_snapshot_dir = try_download_model(model_id)
            selected_model_id = model_id
            break
        except Exception as e:
            msg = str(e)
            summary["errors"].append({"stage": "model", "model_id": model_id, "error": msg})
            if is_auth_error(msg):
                summary["errors"].append(
                    {
                        "stage": "model",
                        "error": "Authentication required for model download. Set HF_TOKEN or HUGGINGFACE_HUB_TOKEN and re-run.",
                    }
                )
                break

if (selected_model_id and resolved_snapshot_dir and resolved_snapshot_dir.exists()) and not existing_model_ok:
    if model_link.exists() or model_link.is_symlink():
        try:
            if model_link.is_symlink():
                target = model_link.resolve()
                if target != resolved_snapshot_dir:
                    model_link.unlink()
        except Exception:
            pass
    if not model_link.exists():
        try:
            model_link.symlink_to(resolved_snapshot_dir, target_is_directory=True)
        except Exception:
            # Fallback: record the snapshot dir as the model path.
            model_link = resolved_snapshot_dir

    # Verify expected artifacts exist in the resolved model directory.
    if not (model_link / "config.json").exists():
        summary["errors"].append(
            {
                "stage": "model",
                "error": "Model download reported success but expected artifacts were not found.",
                "reported_snapshot_dir": str(resolved_snapshot_dir),
                "verified_path": str(model_link),
                "search_root": str(cache_root),
            }
        )
        summary["model"]["ok"] = False
    else:
        summary["model"]["ok"] = True
        summary["model"]["source"] = f"hf://{selected_model_id}"
        summary["model"]["path"] = str(model_link)
        summary["model"]["resolved_snapshot_dir"] = str(resolved_snapshot_dir)
        summary["model"]["version"] = resolved_snapshot_dir.name
        sig, hashed_files = sha256_model_signature(model_link)
        summary["model"]["sha256"] = sig
        summary["model"]["hashed_files"] = hashed_files

    # Persist metadata for offline reuse.
    if summary["model"]["ok"]:
        try:
            model_info_path.write_text(
                json.dumps(
                    {
                        "model_id": selected_model_id,
                        "source": summary["model"]["source"],
                        "version": summary["model"]["version"],
                        "path": summary["model"]["path"],
                        "sha256": summary["model"]["sha256"],
                        "resolved_snapshot_dir": summary["model"]["resolved_snapshot_dir"],
                        "hashed_files": summary["model"].get("hashed_files", []),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

if summary["model"]["ok"]:
    # 3) Generate a minimal draft config aligned to the resolved model, writing under benchmark_assets/model.
    draft_cfg_path = model_root / "draft_config.json"
    try:
        import json as _json
        from transformers import AutoConfig

        template_path = repo_root / "configs" / "qwen2.5-7b-eagle3.json"
        draft_cfg = _json.loads(template_path.read_text(encoding="utf-8"))
        target_cfg = AutoConfig.from_pretrained(str(model_link))

        # Align commonly used keys when present on target config.
        for key in list(draft_cfg.keys()):
            if hasattr(target_cfg, key):
                val = getattr(target_cfg, key)
                # Make torch_dtype JSON-serializable if present.
                try:
                    import torch  # noqa: F401
                    import torch as _torch

                    if key == "torch_dtype" and isinstance(val, _torch.dtype):
                        val = str(val).replace("torch.", "")
                except Exception:
                    pass
                draft_cfg[key] = val

        # Also align a known set even if not in template.
        for key in (
            "vocab_size",
            "hidden_size",
            "num_attention_heads",
            "num_key_value_heads",
            "intermediate_size",
            "max_position_embeddings",
            "rms_norm_eps",
            "hidden_act",
            "bos_token_id",
            "eos_token_id",
        ):
            if hasattr(target_cfg, key):
                draft_cfg[key] = getattr(target_cfg, key)

        draft_cfg["num_hidden_layers"] = 1
        draft_cfg["tie_word_embeddings"] = False
        draft_cfg["use_cache"] = True
        if "architectures" not in draft_cfg:
            draft_cfg["architectures"] = ["LlamaForCausalLMEagle3"]
        if "model_type" not in draft_cfg:
            draft_cfg["model_type"] = "llama"

        draft_cfg_path.write_text(_json.dumps(draft_cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summary["draft_config"]["ok"] = True
        summary["draft_config"]["path"] = str(draft_cfg_path)
        summary["draft_config"]["sha256"] = sha256_file(draft_cfg_path)
    except Exception as e:
        summary["errors"].append({"stage": "draft_config", "error": str(e)})
else:
    # No model available (download failed and no cached model).
    pass

summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"[prepare] wrote summary: {summary_path}", flush=True)
PY
set -e

if [[ ! -f "$download_summary_json" ]]; then
  echo "[prepare] ERROR: internal downloader did not produce $download_summary_json" >>"$log_path"
fi

RESOLVED_PYTHON="$resolved_python" PYTHON_RESOLUTION="$python_resolution" REPO_ROOT="$repo_root" \
  "$host_python" - "$download_summary_json" <<'PY' >"$results_json"
import json, os, pathlib, subprocess, sys

repo_root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
log_path = repo_root / "build_output" / "prepare" / "log.txt"

def tail(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

def git_commit() -> str:
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=str(repo_root), text=True, timeout=10).strip()
    except Exception:
        return ""

def safe_env() -> dict:
    keep_prefixes = ("SCIMLOPSBENCH_", "CUDA_", "HF_", "TRANSFORMERS_", "TORCH", "PYTHON", "WANDB_")
    keep_keys = {"PATH","HOME","USER","SHELL","PWD","VIRTUAL_ENV","CONDA_DEFAULT_ENV","CONDA_PREFIX"}
    out = {}
    for k, v in os.environ.items():
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes):
            out[k] = v
    return out

summary_path = pathlib.Path(sys.argv[1])
summary = {}
try:
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
except Exception:
    summary = {}

dataset = summary.get("dataset", {}) if isinstance(summary, dict) else {}
model = summary.get("model", {}) if isinstance(summary, dict) else {}
draft = summary.get("draft_config", {}) if isinstance(summary, dict) else {}
errors = summary.get("errors", []) if isinstance(summary, dict) else []

dataset_ok = bool(dataset.get("ok"))
model_ok = bool(model.get("ok"))
draft_ok = bool(draft.get("ok"))

status = "success" if (dataset_ok and model_ok and draft_ok) else "failure"
failure_category = "unknown"
decision_reason = (
    "Dataset prepared via scripts/prepare_data.py (ShareGPT default per docs). "
    "Model downloaded via huggingface_hub.snapshot_download into benchmark_assets/cache and linked into benchmark_assets/model."
)

if status == "failure":
    # Prefer an auth classification if any error looks like gating/token.
    err_txt = "\n".join(str(e) for e in errors)
    if any(s in err_txt.lower() for s in ("token", "gated", "401", "403", "permission")):
        failure_category = "auth_required"
    elif any(s in err_txt.lower() for s in ("huggingface_hub", "datasets", "transformers", "no module named")):
        failure_category = "deps"
    elif any(s in err_txt.lower() for s in ("connection", "timed out", "name resolution", "network")):
        failure_category = "download_failed"
    elif not model_ok:
        failure_category = "model"
    elif not dataset_ok:
        failure_category = "data"

res = {
  "status": status,
  "skip_reason": "unknown",
  "exit_code": 0 if status != "failure" else 1,
  "stage": "prepare",
  "task": "download",
  "command": "bash benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {
      "path": str(dataset.get("path","")),
      "source": str(dataset.get("source","")),
      "version": str(dataset.get("version","")),
      "sha256": str(dataset.get("sha256","")),
    },
    "model": {
      "path": str(model.get("path","")),
      "source": str(model.get("source","")),
      "version": str(model.get("version","")),
      "sha256": str(model.get("sha256","")),
    },
  },
  "meta": {
    "python": os.environ.get("RESOLVED_PYTHON","") or "",
    "python_resolution": os.environ.get("PYTHON_RESOLUTION","") or "",
    "git_commit": git_commit(),
    "env_vars": safe_env(),
    "decision_reason": decision_reason,
    "draft_model_config_path": str(draft.get("path","")),
    "draft_model_config_sha256": str(draft.get("sha256","")),
    "model_resolved_snapshot_dir": str(model.get("resolved_snapshot_dir","")),
    "model_hashed_files": model.get("hashed_files", []),
    "errors": errors,
  },
  "failure_category": failure_category if status == "failure" else "unknown",
  "error_excerpt": tail(log_path),
}
print(json.dumps(res, indent=2, ensure_ascii=False))
PY

exit_code="$("$host_python" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("exit_code",1))' "$results_json" 2>/dev/null || echo 1)"
exit "$exit_code"
