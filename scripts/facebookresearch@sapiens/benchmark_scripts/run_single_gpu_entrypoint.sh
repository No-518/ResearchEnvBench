#!/usr/bin/env bash
set -u
set -o pipefail

STAGE="single_gpu"
TASK="train"
FRAMEWORK="pytorch"
TIMEOUT_SEC="${SCIMLOPSBENCH_SINGLE_GPU_TIMEOUT_SEC:-600}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/build_output/${STAGE}"
PREPARE_RESULTS="${REPO_ROOT}/build_output/prepare/results.json"
RUNNER="${REPO_ROOT}/benchmark_scripts/runner.py"

mkdir -p "${OUT_DIR}"
LOG_PATH="${OUT_DIR}/log.txt"
RESULTS_PATH="${OUT_DIR}/results.json"
TS_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || true)"

# Initialize results.json early to avoid stale artifacts on early termination.
cat > "${RESULTS_PATH}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "${STAGE}",
  "task": "${TASK}",
  "command": "benchmark_scripts/run_single_gpu_entrypoint.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "${FRAMEWORK}",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "single_gpu stage placeholder (not completed)",
    "timestamp_utc": "${TS_UTC}",
    "placeholder": true
  },
  "failure_category": "unknown",
  "error_excerpt": "stage did not complete"
}
EOF
: > "${LOG_PATH}"

PY_SYS="$(command -v python3 || command -v python || true)"
if [[ -z "${PY_SYS}" ]]; then
  echo "No python found in PATH to invoke runner." >&2
  cat > "${RESULTS_PATH}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "${STAGE}",
  "task": "${TASK}",
  "command": "benchmark_scripts/run_single_gpu_entrypoint.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "${FRAMEWORK}",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python is required to invoke runner.py",
    "timestamp_utc": "${TS_UTC}"
  },
  "failure_category": "deps",
  "error_excerpt": "python not found in PATH"
}
EOF
  exit 1
fi

if [[ ! -f "${PREPARE_RESULTS}" ]]; then
  "${PY_SYS}" "${RUNNER}" --stage "${STAGE}" --task "${TASK}" --framework "${FRAMEWORK}" --out-dir "${OUT_DIR}" \
    --timeout-sec "${TIMEOUT_SEC}" --failure-category "missing_stage_results" \
    --env "CUDA_VISIBLE_DEVICES=0" -- \
    bash -lc "echo 'Missing ${PREPARE_RESULTS}; run prepare stage first.' >&2; exit 1"
  exit $?
fi

DATASET_DIR="$("${PY_SYS}" - <<'PY' "${PREPARE_RESULTS}" 2>/dev/null || true
import json, sys
p = sys.argv[1]
try:
  data = json.load(open(p, "r", encoding="utf-8"))
  print((data.get("assets") or {}).get("dataset", {}).get("path", "") or "")
except Exception:
  pass
PY
)"
MODEL_CKPT="$("${PY_SYS}" - <<'PY' "${PREPARE_RESULTS}" 2>/dev/null || true
import json, sys, os
p = sys.argv[1]
try:
  data = json.load(open(p, "r", encoding="utf-8"))
  ckpt = (data.get("meta") or {}).get("model_checkpoint_path", "") or ""
  if ckpt and os.path.exists(ckpt):
    print(ckpt); sys.exit(0)
  model_dir = (data.get("assets") or {}).get("model", {}).get("path", "") or ""
  if model_dir and os.path.isdir(model_dir):
    import pathlib
    cand = sorted(pathlib.Path(model_dir).glob("*.pth"))
    if cand:
      print(str(cand[0]))
except Exception:
  pass
PY
)"

if [[ -z "${DATASET_DIR}" || ! -d "${DATASET_DIR}" ]]; then
  "${PY_SYS}" "${RUNNER}" --stage "${STAGE}" --task "${TASK}" --framework "${FRAMEWORK}" --out-dir "${OUT_DIR}" \
    --timeout-sec "${TIMEOUT_SEC}" --assets-from "${PREPARE_RESULTS}" --failure-category "data" \
    --env "CUDA_VISIBLE_DEVICES=0" -- \
    bash -lc "echo 'Dataset path missing or invalid in ${PREPARE_RESULTS}.' >&2; exit 1"
  exit $?
fi

if [[ -z "${MODEL_CKPT}" || ! -f "${MODEL_CKPT}" ]]; then
  "${PY_SYS}" "${RUNNER}" --stage "${STAGE}" --task "${TASK}" --framework "${FRAMEWORK}" --out-dir "${OUT_DIR}" \
    --timeout-sec "${TIMEOUT_SEC}" --assets-from "${PREPARE_RESULTS}" --failure-category "model" \
    --env "CUDA_VISIBLE_DEVICES=0" -- \
    bash -lc "echo 'Model checkpoint missing or invalid (expected from ${PREPARE_RESULTS}).' >&2; exit 1"
  exit $?
fi

WORK_DIR="${OUT_DIR}/work_dir"
mkdir -p "${WORK_DIR}"

DECISION_REASON="Use MMEngine entrypoint seg/tools/train.py with CUDA_VISIBLE_DEVICES=0 on a 1-sample dataset subset with max_epochs=1 and batch_size=1 (=> exactly 1 train iter)."

"${PY_SYS}" "${RUNNER}" \
  --stage "${STAGE}" \
  --task "${TASK}" \
  --framework "${FRAMEWORK}" \
  --out-dir "${OUT_DIR}" \
  --timeout-sec "${TIMEOUT_SEC}" \
  --assets-from "${PREPARE_RESULTS}" \
  --decision-reason "${DECISION_REASON}" \
  --env "CUDA_VISIBLE_DEVICES=0" \
  --min-gpus 1 \
  --use-python -- \
  seg/tools/train.py \
    seg/configs/sapiens_normal/normal_general/sapiens_0.3b_normal_general-1024x768.py \
    --work-dir "${WORK_DIR}" \
    --cfg-options \
      dataset_train.data_root="${DATASET_DIR}" \
      model.backbone.init_cfg.checkpoint="${MODEL_CKPT}" \
      train_dataloader.batch_size=1 \
      train_dataloader.num_workers=0 \
      train_dataloader.persistent_workers=False \
      train_dataloader.dataset.metainfo.from_file="seg/configs/_base_/datasets/render_people.py" \
      norm_cfg.type=BN \
      train_cfg.max_epochs=1 \
      train_cfg.val_interval=999999 \
      default_hooks.logger.interval=1 \
      default_hooks.checkpoint.interval=999999 \
      default_hooks.visualization.interval=999999
