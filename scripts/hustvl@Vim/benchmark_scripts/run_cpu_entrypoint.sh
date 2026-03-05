#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="cpu"
OUT_DIR="${REPO_ROOT}/build_output/${STAGE}"

TIMEOUT_SEC="${SCIMLOPSBENCH_CPU_TIMEOUT_SEC:-600}"
PREPARE_RESULTS="${REPO_ROOT}/build_output/prepare/results.json"

PY_JSON="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"

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

mkdir -p "${OUT_DIR}"

decision_reason="Detectron2 plain_train_net.py 1-iter CPU run on coco_2017_val_100 (downloaded via prepare_assets.sh); evaluation disabled by setting DATASETS.TEST=()."

if [[ -z "${dataset_root}" || -z "${model_path}" || "${prepare_status}" != "success" ]]; then
  "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/runner.py" \
    --stage "${STAGE}" --task train --framework pytorch \
    --timeout-sec "${TIMEOUT_SEC}" --requires-python \
    --failure-category data \
    --decision-reason "prepare stage missing/failed; cannot locate dataset_root/model_path from ${PREPARE_RESULTS}" \
    --out-dir "${OUT_DIR}" \
    --env "CUDA_VISIBLE_DEVICES=" \
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
  --env "CUDA_VISIBLE_DEVICES=" \
  --env "DETECTRON2_DATASETS=${dataset_root}" \
  --env "PYTHONPATH=${REPO_ROOT}/det:${PYTHONPATH:-}" \
  -- "{{python}}" det/tools/plain_train_net.py \
    --num-gpus 0 \
    --config-file det/configs/COCO-Detection/faster_rcnn_R_50_FPN_1x.yaml \
    SOLVER.IMS_PER_BATCH 1 \
    SOLVER.MAX_ITER 1 \
    DATALOADER.NUM_WORKERS 0 \
    TEST.EVAL_PERIOD 0 \
    DATASETS.TRAIN "(\"coco_2017_val_100\",)" \
    DATASETS.TEST "()" \
    MODEL.WEIGHTS "${model_path}" \
    MODEL.DEVICE "cpu" \
    OUTPUT_DIR "${d2_out}"

