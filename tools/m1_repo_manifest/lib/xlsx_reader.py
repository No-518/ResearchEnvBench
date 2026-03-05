from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
import pandas as pd

# canonical field -> possible column names (中英混合，尽量覆盖你现在的表头)
COLUMN_ALIASES: Dict[str, List[str]] = {
    "repo_name": ["repo名字","repo_name","repo","name"],
    "repo_url": ["repo链接","repo_url","url","github","GitHub链接","Github链接"],
    "commit_sha": ["commit_sha","commit","sha","commit hash","commit_sha（你手动填）","commit_sha(手动)"],
    "repo_type": ["repo类型(A,B)","repo_type","type"],
    "hardware_desc": ["CPU/multi GPU","hardware_bucket","hardware","hardware_desc"],
    "eval_dims": ["可以评判能力维度(eg. c0,c1,c2,c3,)","eval_dims","能力维度","c_dims"],
    "difficulty": ["配的时候的难易程度","difficulty","难易程度"],
    "notes": ["备注","notes","comment","问题/环境搭建"],
    "paper": ["论文","paper"],
    "models": ["模型","model","models"],
    "dataset": ["数据集","dataset"],
    "framework": ["框架","framework"],
    "task": ["任务","task"],
    "usable": ["是否可用","usable"],
    "tests_ready": ["是否已完成测试脚本设置。","是否已完成测试脚本设置","tests_ready"],
    "manual_ready": ["是否已经人工配置好","manual_ready"],
    # optional per-repo overrides
    "baseline_targets": ["baseline_targets","baselines","baseline","要跑baseline","要跑哪些baseline"],
    "timeout_agent_sec": ["timeout_agent_sec","agent_timeout","agent_timeout_sec","agent超时(s)","agent超时"],
    "timeout_run_all_sec": ["timeout_run_all_sec","run_all_timeout","run_all_timeout_sec","run_all超时(s)","run_all超时"],
}

REQUIRED_CANONICAL = ["repo_url","commit_sha"]

def _pick_sheet(xlsx_path: str, preferred_sheet: Optional[str]=None) -> str:
    xl = pd.ExcelFile(xlsx_path)
    if preferred_sheet and preferred_sheet in xl.sheet_names:
        return preferred_sheet
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, nrows=3)
        cols = set(map(str, df.columns))
        def has_any(alias_list: Sequence[str]) -> bool:
            return any(a in cols for a in alias_list)
        if all(has_any(COLUMN_ALIASES[k]) for k in REQUIRED_CANONICAL):
            return sheet
    return xl.sheet_names[0]

def _resolve_col(df_cols: Sequence[Any], aliases: Sequence[str]) -> Optional[str]:
    cols = [str(c) for c in df_cols]
    for a in aliases:
        if a in cols:
            return a
    return None

@dataclass
class XlsxTable:
    sheet: str
    df: pd.DataFrame
    colmap: Dict[str, str]  # canonical -> actual column name

def load_table(xlsx_path: str, sheet: Optional[str]=None) -> XlsxTable:
    sheet_name = _pick_sheet(xlsx_path, sheet)
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name)
    colmap: Dict[str,str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        found = _resolve_col(df.columns, aliases)
        if found:
            colmap[canonical] = found
    missing = [k for k in REQUIRED_CANONICAL if k not in colmap]
    if missing:
        raise ValueError(f"XLSX 缺少必要列（可用别名也没匹配到）: {missing}. 当前表头: {list(df.columns)}")
    return XlsxTable(sheet=sheet_name, df=df, colmap=colmap)
