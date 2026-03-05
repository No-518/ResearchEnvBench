# M1 模块：repo 清单与运行配置（XLSX -> Manifest）

这个模块负责把你维护/自动生成的 `repos.xlsx`（repo 列表 + commit + 元数据）转换成 **可复跑、可追溯** 的 `manifest.json`，
供 Host Orchestrator（主程序）读取并展开为 `repo × baseline` 的运行任务。

## 你会得到什么产物
- `manifest.json`：每个 repo 的规范化字段（repo_full_name/repo_url/commit_sha/hardware_bucket/eval_dims/difficulty/notes…）
- （可选）`run_matrix.jsonl`：把每个 repo 按 baseline_targets 展开成 job 列表（主程序可直接遍历）

## 依赖
- Python 3.10+
- pandas, openpyxl（读取 xlsx）

## 快速开始
```bash
python tools/m1_repo_manifest/build_manifest.py \
  --xlsx repos.xlsx \
  --out manifests/manifest.json \
  --default-baselines nexau,codex,claude_code \
  --emit-run-matrix manifests/run_matrix.jsonl
```

## 给主程序的最小接入方式
```python
from tools.m1_repo_manifest.lib.manifest import load_manifest, expand_runs

manifest = load_manifest("manifests/manifest.json")
for job in expand_runs(manifest):
    print(job["job_id"], job["repo_full_name"], job["baseline"])
```

## 最小字段
manifest entry 必含：
- repo_full_name（从 repo_url 解析）
- repo_url
- commit_sha（7-40 位 hex）
- hardware_bucket（cpu/single/multi）
- baseline_targets（列表；可由 --default-baselines 注入）
