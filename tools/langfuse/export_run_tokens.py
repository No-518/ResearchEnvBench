#!/usr/bin/env python3
"""Export aggregated token usage for one benchmark run from Langfuse.

Design goals:
- Stdlib only (no requests dependency).
- Non-fatal by default: if Langfuse isn't configured, writes a stub JSON and exits 0.
- Robust to minor API shape changes: keeps raw response for debugging.

Expected directory layout:
  results/<run_id>/
    jobs/<job_id>/job_summary.json

The time window is derived from min(agent_start) .. max(agent_end) across jobs.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List


def _iso_utc_from_ts(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _load_env_file(path: Path) -> Dict[str, str]:
    """Parse a simple KEY=VALUE .env file.

    - Ignores blank lines and lines starting with '#'.
    - Strips surrounding single/double quotes.
    """
    env: Dict[str, str] = {}
    if not path or not path.exists():
        return env

    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if k:
            env[k] = v
    return env


def _get_langfuse_creds(env: Dict[str, str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    host = (env.get("LANGFUSE_HOST") or env.get("LANGFUSE_BASE_URL") or "").strip()
    pk = (env.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    sk = (env.get("LANGFUSE_SECRET_KEY") or "").strip()

    if host.endswith("/"):
        host = host[:-1]
    if not host:
        return None, None, None
    return host, pk or None, sk or None


def _compute_time_window(run_dir: Path, pad_sec: int) -> Optional[Tuple[float, float]]:
    job_summaries = sorted(run_dir.glob("jobs/*/job_summary.json"))
    starts: List[float] = []
    ends: List[float] = []
    for p in job_summaries:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = d.get("timestamps") if isinstance(d, dict) else None
        if not isinstance(ts, dict):
            continue
        a = ts.get("agent_start")
        b = ts.get("agent_end")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            starts.append(float(a))
            ends.append(float(b))

    if not starts or not ends:
        return None

    start = min(starts) - float(max(0, pad_sec))
    end = max(ends) + float(max(0, pad_sec))
    # Guard against accidental inversion.
    if end <= start:
        end = start + 1.0
    return start, end


def _http_get_json(url: str, *, auth_basic: Optional[str], timeout_sec: int = 60) -> Any:
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "Env-for-research-benchmark/1.0 (langfuse-export)",
    }
    if auth_basic:
        headers["Authorization"] = f"Basic {auth_basic}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read()
        # Try UTF-8 first, fall back to latin1.
        try:
            txt = raw.decode("utf-8")
        except Exception:
            txt = raw.decode("latin1")
        return json.loads(txt)


def _try_extract_first_row(payload: Any) -> Optional[Dict[str, Any]]:
    """Metrics responses are typically {"data": [{...}], ...}.

    We try a few shapes to be resilient.
    """
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            if isinstance(data[0], dict):
                return data[0]
        # Some APIs might return {"result": {...}}
        result = payload.get("result")
        if isinstance(result, dict):
            return result
    return None


def _coerce_number(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


def _extract_metrics_from_row(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    def to_int(x: Any) -> Optional[int]:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x).strip()
        return int(s) if s else None

    def to_float(x: Any) -> Optional[float]:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        return float(s) if s else None

    # Langfuse Metrics API 常见返回：sum_xxx；也兼容旧的 xxx_sum / 平铺字段
    total = (
        row.get("sum_totalTokens")
        or row.get("totalTokens_sum")
        or row.get("totalTokens")
        or row.get("sum_totalUsage")
        or row.get("totalUsage_sum")
        or row.get("totalUsage")
    )
    inp = (
        row.get("sum_inputTokens")
        or row.get("inputTokens_sum")
        or row.get("inputTokens")
        or row.get("sum_promptTokens")
        or row.get("promptTokens_sum")
        or row.get("promptTokens")
        or row.get("sum_inputUsage")
        or row.get("inputUsage_sum")
        or row.get("inputUsage")
    )
    out = (
        row.get("sum_outputTokens")
        or row.get("outputTokens_sum")
        or row.get("outputTokens")
        or row.get("sum_completionTokens")
        or row.get("completionTokens_sum")
        or row.get("completionTokens")
        or row.get("sum_outputUsage")
        or row.get("outputUsage_sum")
        or row.get("outputUsage")
    )
    cost = (
        row.get("sum_totalCost")
        or row.get("totalCost_sum")
        or row.get("totalCost")
        or row.get("sum_cost")
        or row.get("cost_sum")
        or row.get("cost")
    )

    return {
        "total_tokens": to_int(total),
        "input_tokens": to_int(inp),
        "output_tokens": to_int(out),
        "total_cost": to_float(cost),
    }



def _build_query(measure_names: Dict[str, str], from_iso: str, to_iso: str) -> Dict[str, Any]:
    """Build a Langfuse Metrics API query.

    measure_names maps logical names -> Langfuse measure names.
    """
    metrics = [
        {"measure": measure_names["total"], "aggregation": "sum"},
    ]

    # Only include optional measures if provided.
    if measure_names.get("input"):
        metrics.append({"measure": measure_names["input"], "aggregation": "sum"})
    if measure_names.get("output"):
        metrics.append({"measure": measure_names["output"], "aggregation": "sum"})
    if measure_names.get("cost"):
        metrics.append({"measure": measure_names["cost"], "aggregation": "sum"})

    return {
        "view": "observations",
        "dimensions": [],
        "metrics": metrics,
        "filters": [],
        "fromTimestamp": from_iso,
        "toTimestamp": to_iso,
    }


def _query_langfuse_metrics(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    from_iso: str,
    to_iso: str,
    timeout_sec: int,
    max_retries: int,
) -> Dict[str, Any]:
    auth_basic = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")

    # Try a couple of likely measure name sets.
    attempts = [
        # Common in Langfuse docs/examples.
        {"total": "totalTokens", "input": "promptTokens", "output": "completionTokens", "cost": "totalCost"},
        # Alternative naming.
        {"total": "totalTokens", "input": "inputTokens", "output": "outputTokens", "cost": "totalCost"},
        # Minimal.
        {"total": "totalTokens", "input": "", "output": "", "cost": ""},
    ]

    last_err: Optional[str] = None
    for attempt_idx, measures in enumerate(attempts, start=1):
        query = _build_query(measures, from_iso, to_iso)
        url = f"{host}/api/public/metrics?" + urllib.parse.urlencode({"query": json.dumps(query, ensure_ascii=False)})

        for retry in range(max_retries + 1):
            try:
                payload = _http_get_json(url, auth_basic=auth_basic, timeout_sec=timeout_sec)
                row = _try_extract_first_row(payload)
                extracted = _extract_metrics_from_row(row or {})
                out= {
                    "status": "success",
                    "attempt": attempt_idx,
                    "measures": measures,
                    "query": query,
                    "response": payload,
                    "extracted": extracted,
                }
                out.update(extracted)
                return out
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    body = ""
                last_err = f"HTTPError {e.code}: {e.reason}; body={body[:500]}"
                # 4xx from an invalid measure name -> break retries and move to next attempt.
                if 400 <= e.code < 500:
                    break
                # 5xx -> retry.
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = f"URLError/Timeout: {type(e).__name__}: {e}"
            except Exception as e:
                last_err = f"Unexpected: {type(e).__name__}: {e}"

            if retry < max_retries:
                time.sleep(min(30.0, 2.0 ** retry))

        # Next measure-name attempt.

    return {
        "status": "error",
        "error": last_err or "unknown_error",
        "fromTimestamp": from_iso,
        "toTimestamp": to_iso,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export aggregated token usage for a run from Langfuse")
    ap.add_argument("--run-dir", required=True, help="results/<run_id> directory")
    ap.add_argument(
        "--out",
        default=None,
        help="Output JSON path (default: <run_dir>/langfuse_tokens.json)",
    )
    ap.add_argument(
        "--secrets-env-file",
        default=None,
        help="Optional .env file to read LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY",
    )
    ap.add_argument("--pad-sec", type=int, default=60, help="Pad seconds added to time window on both ends")
    ap.add_argument("--timeout-sec", type=int, default=60, help="HTTP timeout seconds")
    ap.add_argument("--max-retries", type=int, default=4, help="Retries per attempt")

    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    out_path = Path(args.out).resolve() if args.out else run_dir / "langfuse_tokens.json"

    env = dict(os.environ)
    if args.secrets_env_file:
        env.update(_load_env_file(Path(args.secrets_env_file)))

    host, pk, sk = _get_langfuse_creds(env)

    result: Dict[str, Any] = {
        "status": "skipped",
        "reason": "langfuse_not_configured",
        "run_dir": str(run_dir),
        "generated_at_utc": _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    tw = _compute_time_window(run_dir, pad_sec=args.pad_sec)
    if not tw:
        result.update({"reason": "missing_agent_timestamps"})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    start_ts, end_ts = tw
    from_iso, to_iso = _iso_utc_from_ts(start_ts), _iso_utc_from_ts(end_ts)
    result.update({"fromTimestamp": from_iso, "toTimestamp": to_iso})

    if not (host and pk and sk):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    metrics = _query_langfuse_metrics(
        host=host,
        public_key=pk,
        secret_key=sk,
        from_iso=from_iso,
        to_iso=to_iso,
        timeout_sec=int(args.timeout_sec),
        max_retries=int(args.max_retries),
    )

    result = {
        "status": metrics.get("status", "error"),
        "run_dir": str(run_dir),
        "langfuse_host": host,
        "fromTimestamp": from_iso,
        "toTimestamp": to_iso,
        "generated_at_utc": _dt.datetime.now(tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        **({"error": metrics.get("error")} if metrics.get("status") != "success" else {}),
        # Keep full payload for debugging, but downstream summary only reads `extracted`.
        "metrics": metrics,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Always return 0 to avoid breaking the benchmark pipeline.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
