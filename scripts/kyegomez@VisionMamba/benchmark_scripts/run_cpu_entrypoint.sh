#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/cpu"
assets_manifest="$repo_root/benchmark_assets/assets.json"
mkdir -p "$stage_dir"

python3_bin="$(command -v python3 || command -v python)"

if [[ ! -f "$assets_manifest" ]]; then
  "$python3_bin" "$repo_root/benchmark_scripts/runner.py" \
    --stage cpu \
    --task infer \
    --out-dir "$stage_dir" \
    --timeout-sec 600 \
    --framework pytorch \
    --failure-category data \
    --decision-reason "Missing benchmark_assets/assets.json; run prepare_assets.sh first." \
    -- bash -lc "echo 'Missing assets manifest: $assets_manifest' >&2; exit 1"
  exit 1
fi

cpu_code=$'import os\nimport runpy\nimport sys\n\nimport torch\n\n# Ensure the repo root is importable.\nrepo_root = os.getcwd()\nif repo_root not in sys.path:\n    sys.path.insert(0, repo_root)\n\n# Force CPU even if CUDA is present.\ntry:\n    if hasattr(torch, \"set_default_device\"):\n        torch.set_default_device(\"cpu\")\nexcept Exception:\n    pass\n\n# Patch the repo example to run with the current implementation.\n# The repo\'s example/README passes `heads=...`, but VisionEncoderMambaBlock.__init__\n# does not accept it; Vim forwards **kwargs to the block. Ignore unsupported kwargs.\ntry:\n    import vision_mamba.model as vm\n\n    _orig_init = vm.VisionEncoderMambaBlock.__init__\n\n    def _patched_init(self, dim, dt_rank, dim_inner, d_state, *args, **kwargs):\n        kwargs.pop(\"heads\", None)\n        return _orig_init(self, dim=dim, dt_rank=dt_rank, dim_inner=dim_inner, d_state=d_state)\n\n    vm.VisionEncoderMambaBlock.__init__ = _patched_init\n\n    # Avoid dumping huge tensors to stdout in the repo\'s debug prints.\n    from einops import rearrange\n\n    def _process_direction(self, x, conv1d, ssm):\n        x = rearrange(x, \"b s d -> b d s\")\n        x = self.softplus(conv1d(x))\n        x = rearrange(x, \"b d s -> b s d\")\n        x = ssm(x)\n        return x\n\n    vm.VisionEncoderMambaBlock.process_direction = _process_direction\nexcept Exception:\n    pass\n\nrunpy.run_path(\"example.py\", run_name=\"__main__\")\n'

"$python3_bin" "$repo_root/benchmark_scripts/runner.py" \
  --stage cpu \
  --task infer \
  --out-dir "$stage_dir" \
  --timeout-sec 600 \
  --framework pytorch \
  --requires-python \
  --assets-json "$assets_manifest" \
  --decision-reason "Repo has no training CLI; run one forward-pass inference via example.py, forcing CPU with CUDA_VISIBLE_DEVICES='' and torch.set_default_device('cpu') when available." \
  --env CUDA_VISIBLE_DEVICES= \
  --python-code "$cpu_code"
