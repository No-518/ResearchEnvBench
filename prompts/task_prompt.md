Repository root is provided by the environment variable:
  REPO_ROOT

First, confirm and enter repo root:
  cd "$REPO_ROOT"
  pwd
  ls

Task:
1) Inspect repository dependency metadata:
   - pyproject.toml / requirements*.txt / environment.yml / setup.py / README
2) Choose an environment tool (prefer conda or uv if reasonable).
3) Create an isolated environment with a clear name (env_name).
4) Install dependencies reproducibly.
5) Verify:
   - The final interpreter path (absolute)
   - Basic imports for the repo and key ML deps
   - Torch availability (if installed)
   - CUDA availability and GPU count (if torch installed and GPU present)

What will be evaluated after you finish (you do NOT need to pick scripts):
- C0: Importability / dependency closure (Pyright missing-import diagnostics)
- C1: Minimal CPU execution (repo-specific fixed script)
- C2: GPU visibility (e.g., torch.cuda.is_available)
- C3: Minimal single-GPU execution (repo-specific fixed script)
- C4: Minimal multi-GPU execution (repo-specific fixed script, if applicable)
- C5: Report consistency validation (hallucination checks)

Important:
- You MUST NOT invent any “entrypoint path” or “cpu_command” fields. The evaluator uses fixed benchmark scripts.
- Your only mandatory artifact is /opt/scimlopsbench/report.json (see below).

Verification commands (examples; adapt as needed):
- python -V
- python -c "import sys; print(sys.executable); print(sys.version)"
- python -c "import importlib; import pkgutil; print('ok')"
- python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.device_count())"

DDP expectation:
- If torch is installed and gpu_count >= 2, you may run a minimal torch.distributed sanity check ONLY if it is lightweight.
- If you do not run any distributed command, set ddp_expected_ok to null and explain.

Mandatory output:
Write /opt/scimlopsbench/report.json using the final interpreter you configured.
You MUST generate the report from verified runtime values, not by guessing.

Use this exact pattern to write the report (edit env_tool/env_name/notes, keep the runtime queries):

python - <<'PY'
import json, sys
report_path = "/opt/scimlopsbench/report.json"

python_path = sys.executable
python_version = sys.version.split()[0]

torch_version = None
cuda_available = None
gpu_count = None
try:
    import torch
    torch_version = getattr(torch, "__version__", None)
    cuda_available = bool(torch.cuda.is_available())
    gpu_count = int(torch.cuda.device_count()) if cuda_available else 0
except Exception:
    pass

report = {
  "python_path": python_path,
  "python_version": python_version,
  "torch_version": torch_version,
  "cuda_available": cuda_available,
  "gpu_count": gpu_count,
  "ddp_expected_ok": None,
  "env_tool": "conda",
  "env_name": "REPLACE_ME",
  "notes": "REPLACE_ME_WITH_KEY_COMMANDS_AND_DECISIONS"
}

with open(report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("WROTE", report_path)
PY

Finally, validate the JSON file exists and is parseable:
  ls -l /opt/scimlopsbench/report.json
  python -c "import json; json.load(open('/opt/scimlopsbench/report.json','r',encoding='utf-8')); print('report.json OK')"
