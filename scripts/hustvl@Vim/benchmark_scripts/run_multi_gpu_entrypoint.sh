#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="multi_gpu"
OUT_DIR="${REPO_ROOT}/build_output/${STAGE}"

TIMEOUT_SEC="${SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC:-1200}"
PREPARE_RESULTS="${REPO_ROOT}/build_output/prepare/results.json"

PY_JSON="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"

devices="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"

mkdir -p "${OUT_DIR}"

gpu_count=0
if command -v nvidia-smi >/dev/null 2>&1; then
  gpu_count="$( (nvidia-smi -L 2>/dev/null || true) | wc -l | awk '{print $1}')"
else
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  py_path="$("${PY_JSON}" - <<'PY' "${report_path}" 2>/dev/null || true
import json, sys
print(json.load(open(sys.argv[1], "r", encoding="utf-8")).get("python_path","") or "")
PY
  )"
  if [[ -n "${py_path}" && -x "${py_path}" ]]; then
    gpu_count="$("${py_path}" - <<'PY' 2>/dev/null || echo 0
import torch
print(torch.cuda.device_count())
PY
    )"
  fi
fi

if [[ "${gpu_count}" -lt 2 ]]; then
  "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/runner.py" \
    --stage "${STAGE}" --task train --framework pytorch \
    --timeout-sec "${TIMEOUT_SEC}" --requires-python \
    --failure-category unknown \
    --decision-reason "Insufficient hardware: detected gpu_count=${gpu_count} (<2 required for multi-GPU). devices=${devices}" \
    --out-dir "${OUT_DIR}" \
    --env "CUDA_VISIBLE_DEVICES=${devices}" \
    -- "{{python}}" -c "raise SystemExit(1)"
  exit $?
fi

dataset_root=""
model_path=""
prepare_status=""
if [[ -f "${PREPARE_RESULTS}" ]]; then
  dataset_root="$("${PY_JSON}" - <<'PY' "${PREPARE_RESULTS}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print((obj.get("assets", {}).get("dataset", {}) or {}).get("path", "") or "")
PY
  )"
  model_path="$("${PY_JSON}" - <<'PY' "${PREPARE_RESULTS}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print((obj.get("assets", {}).get("model", {}) or {}).get("path", "") or "")
PY
  )"
  prepare_status="$("${PY_JSON}" - <<'PY' "${PREPARE_RESULTS}"
import json, sys
obj=json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(obj.get("status","") or "")
PY
  )"
fi

decision_reason="Detectron2 plain_train_net.py 1-iter multi-GPU run on coco_2017_val_100 with CUDA_VISIBLE_DEVICES=${devices}; evaluation disabled by setting DATASETS.TEST=()."

if [[ -z "${dataset_root}" || -z "${model_path}" || "${prepare_status}" != "success" ]]; then
  "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/runner.py" \
    --stage "${STAGE}" --task train --framework pytorch \
    --timeout-sec "${TIMEOUT_SEC}" --requires-python \
    --failure-category data \
    --decision-reason "prepare stage missing/failed; cannot locate dataset_root/model_path from ${PREPARE_RESULTS}" \
    --out-dir "${OUT_DIR}" \
    --env "CUDA_VISIBLE_DEVICES=${devices}" \
    --env "PYTHONPATH=${REPO_ROOT}/det:${PYTHONPATH:-}" \
    -- "{{python}}" -c "raise SystemExit(1)"
  exit $?
fi

d2_out="${OUT_DIR}/detectron2_output"

"${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/runner.py" \
  --stage "${STAGE}" --task train --framework pytorch \
  --timeout-sec "${TIMEOUT_SEC}" --requires-python \
  --decision-reason "${decision_reason}" \
  --out-dir "${OUT_DIR}" \
  --env "CUDA_VISIBLE_DEVICES=${devices}" \
  --env "DETECTRON2_DATASETS=${dataset_root}" \
  --env "PYTHONPATH=${REPO_ROOT}/det:${PYTHONPATH:-}" \
  -- "{{python}}" det/tools/plain_train_net.py \
    --num-gpus 2 \
    --config-file det/configs/COCO-Detection/faster_rcnn_R_50_FPN_1x.yaml \
    SOLVER.IMS_PER_BATCH 1 \
    SOLVER.MAX_ITER 1 \
    DATALOADER.NUM_WORKERS 0 \
    TEST.EVAL_PERIOD 0 \
    DATASETS.TRAIN "(\"coco_2017_val_100\",)" \
    DATASETS.TEST "()" \
    MODEL.WEIGHTS "${model_path}" \
    MODEL.DEVICE "cuda" \
    OUTPUT_DIR "${d2_out}"
