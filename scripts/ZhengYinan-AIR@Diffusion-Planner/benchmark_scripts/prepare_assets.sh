#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets:
  - minimal dataset (synthetic, in Diffusion-Planner training format)
  - minimal model download (HF checkpoint files from README)

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets directories (created if missing):
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Optional:
  --report-path <path>      Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>           Explicit python to use for dataset generation (highest)
  --timeout-sec <int>       Default: 1200 (best-effort; no hard kill without coreutils timeout)

Environment:
  HF_AUTH_TOKEN / HF_TOKEN  (optional) used for authenticated downloads if required
EOF
}

report_path=""
python_bin=""
timeout_sec="1200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

stage_dir="${repo_root}/build_output/prepare"
mkdir -p "$stage_dir"
log_file="${stage_dir}/log.txt"
results_json="${stage_dir}/results.json"

: >"$log_file"
exec > >(tee -a "$log_file") 2>&1

report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

resolve_python() {
  if [[ -n "${python_bin}" ]]; then
    echo "${python_bin}"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "${SCIMLOPSBENCH_PYTHON}"
    return 0
  fi
  python3 - <<PY
import json, sys
from pathlib import Path
p = Path(${report_path@Q})
if not p.exists():
  print("")
  sys.exit(1)
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("")
  sys.exit(1)
py = data.get("python_path")
if not isinstance(py, str) or not py.strip():
  print("")
  sys.exit(1)
print(py)
PY
}

status="success"
skip_reason="not_applicable"
failure_category="unknown"
command="benchmark_scripts/prepare_assets.sh"

dataset_source="generated:diffusion_planner_synth"
dataset_version="v1"
model_source="https://huggingface.co/ZhengYinan2001/Diffusion-Planner"
model_version="main"

cache_dir="${repo_root}/benchmark_assets/cache"
dataset_cache_dir="${cache_dir}/dataset"
model_cache_dir="${cache_dir}/model"
dataset_dir="${repo_root}/benchmark_assets/dataset"
model_dir="${repo_root}/benchmark_assets/model/checkpoints"
mkdir -p "$dataset_cache_dir" "$model_cache_dir" "$dataset_dir" "$model_dir"

dataset_npz_cache="${dataset_cache_dir}/sample_0.npz"
dataset_list_cache="${dataset_cache_dir}/train_list.json"
dataset_npz="${dataset_dir}/sample_0.npz"
dataset_list="${dataset_dir}/train_list.json"

model_args_url="https://huggingface.co/ZhengYinan2001/Diffusion-Planner/resolve/main/args.json"
model_pth_url="https://huggingface.co/ZhengYinan2001/Diffusion-Planner/resolve/main/model.pth"
model_args_cache="${model_cache_dir}/args.json"
model_pth_cache="${model_cache_dir}/model.pth"
model_args="${model_dir}/args.json"
model_pth="${model_dir}/model.pth"

finalize() {
  local rc="$1"
  trap - EXIT

  if [[ "$rc" -ne 0 && "${status}" != "failure" ]]; then
    status="failure"
    failure_category="${failure_category:-unknown}"
  fi

  local dataset_sha=""
  local model_sha=""
  local model_args_sha=""
  if [[ -f "$dataset_npz" ]]; then
    dataset_sha="$(compute_sha256 "$dataset_npz" 2>/dev/null || true)"
  fi
  if [[ -f "$model_pth" ]]; then
    model_sha="$(compute_sha256 "$model_pth" 2>/dev/null || true)"
  fi
  if [[ -f "$model_args" ]]; then
    model_args_sha="$(compute_sha256 "$model_args" 2>/dev/null || true)"
  fi

  STATUS="$status" \
  SKIP_REASON="$skip_reason" \
  TIMEOUT_SEC="$timeout_sec" \
  COMMAND_STR="$command" \
  DATASET_DIR="$dataset_dir" \
  DATASET_SOURCE="$dataset_source" \
  DATASET_VERSION="$dataset_version" \
  DATASET_SHA256="$dataset_sha" \
  MODEL_DIR="$model_dir" \
  MODEL_SOURCE="$model_source" \
  MODEL_VERSION="$model_version" \
  MODEL_SHA256="$model_sha" \
  PYTHON_RESOLVED="$python_resolved" \
  REPORT_PATH="$report_path" \
  MODEL_ARGS_URL="$model_args_url" \
  MODEL_PTH_URL="$model_pth_url" \
  MODEL_ARGS_PATH="$model_args" \
  MODEL_PTH_PATH="$model_pth" \
  MODEL_ARGS_SHA256="$model_args_sha" \
  FAILURE_CATEGORY="$failure_category" \
  LOG_FILE="$log_file" \
  RESULTS_JSON="$results_json" \
    python3 - <<'PY'
import json
import os
from pathlib import Path


def tail_file(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception as e:
        return f"[prepare] failed to read log: {e}"


status = os.environ.get("STATUS", "failure")
exit_code = 0 if status != "failure" else 1

error_excerpt = tail_file(Path(os.environ.get("LOG_FILE", "")))

payload = {
    "status": status,
    "skip_reason": os.environ.get("SKIP_REASON", "not_applicable"),
    "exit_code": exit_code,
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "1200") or 1200),
    "framework": "pytorch",
    "assets": {
        "dataset": {
            "path": os.environ.get("DATASET_DIR", ""),
            "source": os.environ.get("DATASET_SOURCE", ""),
            "version": os.environ.get("DATASET_VERSION", ""),
            "sha256": os.environ.get("DATASET_SHA256", ""),
        },
        "model": {
            "path": os.environ.get("MODEL_DIR", ""),
            "source": os.environ.get("MODEL_SOURCE", ""),
            "version": os.environ.get("MODEL_VERSION", ""),
            "sha256": os.environ.get("MODEL_SHA256", ""),
        },
    },
    "meta": {
        "python": os.environ.get("PYTHON_RESOLVED", ""),
        "report_path": os.environ.get("REPORT_PATH", ""),
        "model_files": {
            "args_json": {
                "url": os.environ.get("MODEL_ARGS_URL", ""),
                "path": os.environ.get("MODEL_ARGS_PATH", ""),
                "sha256": os.environ.get("MODEL_ARGS_SHA256", ""),
            },
            "model_pth": {
                "url": os.environ.get("MODEL_PTH_URL", ""),
                "path": os.environ.get("MODEL_PTH_PATH", ""),
                "sha256": os.environ.get("MODEL_SHA256", ""),
            },
        },
        "decision_reason": "Dataset: generated tiny synthetic .npz matching DiffusionPlannerData keys; Model: downloaded public HF checkpoint files referenced in README.",
        "env_vars": {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_") or k.startswith("HF_") or k.startswith("CUDA")},
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": error_excerpt,
}

Path(os.environ["RESULTS_JSON"]).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
PY

  if [[ "${status}" == "failure" ]]; then
    exit 1
  fi
  exit 0
}

trap 'finalize $?' EXIT

sha_file() { echo "$1.sha256"; }

compute_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

ensure_sha_sidecar() {
  local path="$1"
  local sidecar
  sidecar="$(sha_file "$path")"
  if [[ ! -f "$path" ]]; then
    return 1
  fi

  if [[ -f "$sidecar" ]]; then
    local current recorded
    current="$(compute_sha256 "$path" 2>/dev/null || true)"
    recorded="$(awk '{print $1}' "$sidecar" 2>/dev/null || true)"
    if [[ -n "$current" && -n "$recorded" && "$current" == "$recorded" ]]; then
      return 0
    fi
    echo "[prepare] sha mismatch for $path (recorded=${recorded:-<missing>} current=${current:-<missing>})"
    return 1
  fi

  compute_sha256 "$path" >"$sidecar"
  return 0
}

download_with_optional_token() {
  local url="$1"
  local dest="$2"
  local tmp="${dest}.tmp"

  if [[ -f "$dest" ]]; then
    if ensure_sha_sidecar "$dest"; then
      echo "[prepare] cache hit (sha match): $dest"
      return 0
    fi
    echo "[prepare] cache present but sha missing/mismatch; will attempt re-download: $dest"
  fi

local auth_header=()
if [[ -n "${HF_AUTH_TOKEN:-}" ]]; then
  auth_header=(-H "Authorization: Bearer ${HF_AUTH_TOKEN}")
elif [[ -n "${HF_TOKEN:-}" ]]; then
  auth_header=(-H "Authorization: Bearer ${HF_TOKEN}")
fi

echo "[prepare] downloading: $url -> $dest"
set +e
curl -L --fail --retry 3 --connect-timeout 10 --max-time 600 "${auth_header[@]}" -o "$tmp" "$url"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  rm -f "$tmp" || true
  echo "[prepare] download failed (rc=$rc): $url"
  return $rc
  fi
  mv -f "$tmp" "$dest"
  compute_sha256 "$dest" >"$(sha_file "$dest")" 2>/dev/null || true
  return 0
}

python_resolved=""
set +e
python_resolved="$(resolve_python)"
py_rc=$?
set -e

if [[ $py_rc -ne 0 || -z "$python_resolved" ]]; then
  echo "[prepare] ERROR: could not resolve python from report/env/--python (report_path=$report_path)"
  failure_category="missing_report"
  status="failure"
else
  echo "[prepare] python=$python_resolved"

  # 1) Dataset: generate minimal synthetic sample in cache (then copy to dataset/)
  if [[ -f "$dataset_npz_cache" && -f "$dataset_list_cache" ]]; then
    ensure_sha_sidecar "$dataset_npz_cache" || true
    echo "[prepare] dataset cache exists"
  else
    echo "[prepare] generating synthetic dataset in cache"
    set +e
    "$python_resolved" - <<'PY'
import json
import numpy as np
from pathlib import Path

repo_root = Path.cwd()
dataset_cache_dir = repo_root / "benchmark_assets" / "cache" / "dataset"
dataset_cache_dir.mkdir(parents=True, exist_ok=True)

agent_num = 2
predicted_neighbor_num = 1
time_len = 2
future_len = 8

lane_num = 2
lane_len = 4
route_num = 1
static_objects_num = 1

ego_current_state = np.zeros((10,), dtype=np.float32)
ego_agent_future = np.zeros((future_len, 3), dtype=np.float32)

neighbor_agents_past = np.zeros((agent_num, time_len, 11), dtype=np.float32)
neighbor_agents_future = np.zeros((agent_num, future_len, 3), dtype=np.float32)

lanes = np.zeros((lane_num, lane_len, 12), dtype=np.float32)
lanes_speed_limit = np.zeros((lane_num, 1), dtype=np.float32)
lanes_has_speed_limit = np.zeros((lane_num, 1), dtype=bool)

route_lanes = np.zeros((route_num, lane_len, 12), dtype=np.float32)
route_lanes_speed_limit = np.zeros((route_num, 1), dtype=np.float32)
route_lanes_has_speed_limit = np.zeros((route_num, 1), dtype=bool)

static_objects = np.zeros((static_objects_num, 10), dtype=np.float32)

sample_path = dataset_cache_dir / "sample_0.npz"
np.savez(
    sample_path,
    ego_current_state=ego_current_state,
    ego_agent_future=ego_agent_future,
    neighbor_agents_past=neighbor_agents_past,
    neighbor_agents_future=neighbor_agents_future,
    lanes=lanes,
    lanes_speed_limit=lanes_speed_limit,
    lanes_has_speed_limit=lanes_has_speed_limit,
    route_lanes=route_lanes,
    route_lanes_speed_limit=route_lanes_speed_limit,
    route_lanes_has_speed_limit=route_lanes_has_speed_limit,
    static_objects=static_objects,
)

list_path = dataset_cache_dir / "train_list.json"
list_path.write_text(json.dumps(["sample_0.npz"], indent=2), encoding="utf-8")
print(f"Wrote {sample_path} and {list_path}")
PY
    gen_rc=$?
    set -e
    if [[ $gen_rc -ne 0 ]]; then
      echo "[prepare] ERROR: synthetic dataset generation failed (rc=$gen_rc)"
      failure_category="data"
      status="failure"
    else
      ensure_sha_sidecar "$dataset_npz_cache" || true
    fi
  fi

  if [[ "${status}" != "failure" ]]; then
    cp -f "$dataset_npz_cache" "$dataset_npz"
    cp -f "$dataset_list_cache" "$dataset_list"
  fi

  # 2) Model: download checkpoint files into cache (then copy to model/)
  if [[ "${status}" != "failure" ]]; then
    model_download_cmds=(
      "curl -L --fail ... -o ${model_args_cache} ${model_args_url}"
      "curl -L --fail ... -o ${model_pth_cache} ${model_pth_url}"
    )
    set +e
    download_with_optional_token "$model_args_url" "$model_args_cache"
    rc_args=$?
    download_with_optional_token "$model_pth_url" "$model_pth_cache"
    rc_pth=$?
    set -e

    if [[ $rc_args -ne 0 || $rc_pth -ne 0 ]]; then
      # Offline reuse allowed if cache already exists.
      if [[ -f "$model_args_cache" && -f "$model_pth_cache" ]]; then
        echo "[prepare] download failed but cache exists; proceeding with cached model"
      else
        echo "[prepare] ERROR: model download failed and no cache available"
        failure_category="download_failed"
        status="failure"
      fi
    fi

    if [[ "${status}" != "failure" ]]; then
      cp -f "$model_args_cache" "$model_args"
      cp -f "$model_pth_cache" "$model_pth"
      if [[ ! -f "$model_args" || ! -f "$model_pth" ]]; then
        echo "[prepare] ERROR: downloader reported success but model files not found in resolved dir: $model_dir"
        failure_category="model"
        status="failure"
      fi
    fi
  fi
fi

exit 0
