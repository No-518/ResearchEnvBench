#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) into:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Overrides (optional):
  SCIMLOPSBENCH_MODEL_ID            HuggingFace repo id (default: hf-internal-testing/tiny-random-LlamaForCausalLM)
  SCIMLOPSBENCH_MODEL_REVISION      HF revision (default: main)
  SCIMLOPSBENCH_OFFLINE             If "1", do not attempt network downloads
  HF_AUTH_TOKEN / HF_TOKEN          Used automatically by huggingface_hub if needed

The selected model must be compatible with `examples/llama.py`.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"

cache_dir="$repo_root/benchmark_assets/cache"
dataset_dir="$repo_root/benchmark_assets/dataset"
model_dir="$repo_root/benchmark_assets/model"
mkdir -p "$cache_dir" "$dataset_dir" "$model_dir"

impl_py="$stage_dir/_prepare_assets_impl.py"
extra_json="$stage_dir/extra.json"

cat >"$impl_py" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dir(root: Path) -> str:
    h = hashlib.sha256()
    for file_path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = file_path.relative_to(root).as_posix().encode("utf-8", errors="replace")
        h.update(rel)
        h.update(b"\0")
        h.update(str(file_path.stat().st_size).encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(file_path).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def safe_name(s: str) -> str:
    return s.replace("/", "__").replace(":", "__")


def load_prev_hash(results_json: Path, key: str) -> str:
    try:
        data = json.loads(results_json.read_text(encoding="utf-8"))
        return str(data.get("assets", {}).get(key, {}).get("sha256", "") or "")
    except Exception:
        return ""


def verify_model_dir(path: Path) -> Tuple[bool, str]:
    if not path.exists() or not path.is_dir():
        return False, f"model dir missing: {path}"
    config = path / "config.json"
    if not config.exists():
        return False, "config.json missing"
    weight_candidates = [
        "model.safetensors",
        "pytorch_model.bin",
        "pytorch_model.bin.index.json",
        "model.safetensors.index.json",
    ]
    if not any((path / c).exists() for c in weight_candidates):
        return False, f"no weights found (expected one of: {', '.join(weight_candidates)})"
    # Tokenizer artifacts: allow either tokenizer.json or tokenizer.model.
    tok_ok = any((path / c).exists() for c in ["tokenizer.json", "tokenizer.model", "tokenizer_config.json"])
    if not tok_ok:
        return False, "tokenizer artifacts missing (expected tokenizer.json/model/config)"
    return True, ""


def main() -> int:
    repo_root = Path(os.environ["REPO_ROOT"]).resolve()
    cache_dir = Path(os.environ["CACHE_DIR"]).resolve()
    dataset_dir = Path(os.environ["DATASET_DIR"]).resolve()
    model_dir = Path(os.environ["MODEL_DIR"]).resolve()
    stage_dir = Path(os.environ["STAGE_DIR"]).resolve()
    extra_json_path = Path(os.environ["EXTRA_JSON"]).resolve()

    offline = os.environ.get("SCIMLOPSBENCH_OFFLINE", "0") == "1"
    model_id = os.environ.get("SCIMLOPSBENCH_MODEL_ID", "hf-internal-testing/tiny-random-LlamaForCausalLM")
    revision = os.environ.get("SCIMLOPSBENCH_MODEL_REVISION", "main")

    if model_id.startswith("meta-llama/"):
        print(
            "Refusing to download meta-llama/* by default (large/restricted). "
            "Set SCIMLOPSBENCH_MODEL_ID to a small public model.",
            file=sys.stderr,
        )
        extra_json_path.write_text(
            json.dumps(
                {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": "auth_required",
                    "meta": {"decision_reason": "meta-llama models are disallowed by default for this benchmark run."},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    # Dataset: repo has no dataset-driven entrypoint; prepare a minimal prompt set anyway.
    dataset_cache_dir = cache_dir / "dataset"
    dataset_cache_dir.mkdir(parents=True, exist_ok=True)
    dataset_cache_path = dataset_cache_dir / "prompts.txt"
    dataset_cache_path.write_text("how are you?\nwhat is MoBA?\n", encoding="utf-8")
    dataset_path = dataset_dir / "prompts.txt"
    shutil.copy2(dataset_cache_path, dataset_path)
    dataset_sha = sha256_file(dataset_path)
    dataset_asset = {"path": str(dataset_path), "source": "generated", "version": "1", "sha256": dataset_sha}

    # Model: download to cache/ then copy to benchmark_assets/model/.
    model_cache_root = cache_dir / "model"
    model_cache_root.mkdir(parents=True, exist_ok=True)
    model_cache_path = model_cache_root / f"{safe_name(model_id)}__{safe_name(revision)}"
    model_target_path = model_dir / f"{safe_name(model_id)}__{safe_name(revision)}"
    prev_results = stage_dir / "results.json"
    prev_model_sha = load_prev_hash(prev_results, "model")

    # If already prepared and matches previous hash, reuse without download.
    if model_target_path.exists():
        ok, err = verify_model_dir(model_target_path)
        if ok:
            cur_sha = sha256_dir(model_target_path)
            if prev_model_sha and cur_sha == prev_model_sha:
                extra_json_path.write_text(
                    json.dumps(
                        {
                            "assets": {
                                "dataset": {
                                    "path": str(dataset_path),
                                    "source": "generated",
                                    "version": "1",
                                    "sha256": dataset_sha,
                                },
                                "model": {
                                    "path": str(model_target_path),
                                    "source": f"hf:{model_id}",
                                    "version": revision,
                                    "sha256": cur_sha,
                                },
                            },
                            "meta": {
                                "decision_reason": "Reused cached model+dataset (sha256 match).",
                                "model_id": model_id,
                                "model_revision": revision,
                                "offline": offline,
                            },
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                print(f"Reused model at {model_target_path}")
                return 0
        else:
            print(f"Existing model dir invalid: {err}; will re-prepare.", file=sys.stderr)

    def _looks_like_network_issue(text: str) -> bool:
        markers = [
            "Temporary failure",
            "Name or service not known",
            "ConnectionError",
            "ReadTimeout",
            "timed out",
            "CERTIFICATE_VERIFY_FAILED",
            "SSLError",
            "ProxyError",
            "Network is unreachable",
            "Connection reset by peer",
        ]
        return any(m in text for m in markers)

    def _pip_install(packages: list[str]) -> Tuple[int, str]:
        cmd = [sys.executable, "-m", "pip", "install", "-q", *packages]
        env = os.environ.copy()
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
        out = (proc.stdout or "") + (proc.stderr or "")
        # Echo for runner log.
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        return int(proc.returncode), out

    # Pre-install required HF packages if missing (requested by benchmark).
    hf_install_meta: Dict[str, object] = {
        "hf_pip_install_attempted": False,
        "hf_pip_install_command": "",
        "hf_pip_install_returncode": None,
        "hf_missing_modules": {},
    }
    missing_pkgs: list[str] = []
    missing_mods: Dict[str, str] = {}
    for mod, pip_name in [
        ("huggingface_hub", "huggingface_hub"),
        ("transformers", "transformers"),
        ("accelerate", "accelerate"),
    ]:
        try:
            __import__(mod)
        except Exception as e:
            missing_pkgs.append(pip_name)
            missing_mods[mod] = str(e)
    hf_install_meta["hf_missing_modules"] = missing_mods

    if missing_pkgs:
        hf_install_meta["hf_pip_install_attempted"] = True
        hf_install_meta["hf_pip_install_command"] = " ".join([sys.executable, "-m", "pip", "install", "-q", *missing_pkgs])
        rc, out = _pip_install(missing_pkgs)
        hf_install_meta["hf_pip_install_returncode"] = rc
        if rc != 0:
            failure_category = "download_failed" if (offline or _looks_like_network_issue(out)) else "deps"
            extra_json_path.write_text(
                json.dumps(
                    {
                        "failure_category": failure_category,
                        "assets": {"dataset": dataset_asset, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
                        "meta": {
                            "decision_reason": f"Failed to pip install required HF packages: {missing_pkgs}",
                            "model_id": model_id,
                            "offline": offline,
                            **hf_install_meta,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return 1

    # Download (or offline lookup) via huggingface_hub to a known local_dir under cache/.
    try:
        from huggingface_hub import snapshot_download
        try:
            from huggingface_hub.utils import HfHubHTTPError as HubHTTPError
        except Exception:
            # Older/alternate symbol name in some environments.
            from huggingface_hub.utils import HFHubHTTPError as HubHTTPError  # type: ignore
    except Exception as e:
        extra_json_path.write_text(
            json.dumps(
                {
                    "failure_category": "deps",
                    "assets": {"dataset": dataset_asset, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
                    "meta": {
                        "decision_reason": f"huggingface_hub not usable even after pip install attempt: {e}",
                        "model_id": model_id,
                        "offline": offline,
                        **hf_install_meta,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    model_cache_path.mkdir(parents=True, exist_ok=True)

    try:
        local_dir = snapshot_download(
            repo_id=model_id,
            revision=revision,
            cache_dir=str(cache_dir / "hf_hub_cache"),
            local_dir=str(model_cache_path),
            local_dir_use_symlinks=False,
            resume_download=True,
            local_files_only=offline,
        )
    except HubHTTPError as e:
        msg = str(e)
        failure_category = "auth_required" if "401" in msg or "403" in msg else "download_failed"
        extra_json_path.write_text(
            json.dumps(
                {
                    "failure_category": failure_category,
                    "assets": {"dataset": dataset_asset, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
                    "meta": {
                        "decision_reason": f"HF download failed: {msg}",
                        "model_id": model_id,
                        "offline": offline,
                        **hf_install_meta,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1
    except Exception as e:
        extra_json_path.write_text(
            json.dumps(
                {
                    "failure_category": "download_failed",
                    "assets": {"dataset": dataset_asset, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
                    "meta": {
                        "decision_reason": f"HF download failed: {e}",
                        "model_id": model_id,
                        "offline": offline,
                        **hf_install_meta,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    resolved_cache_dir = Path(local_dir).resolve()
    if not resolved_cache_dir.exists():
        extra_json_path.write_text(
            json.dumps(
                {
                    "failure_category": "model",
                    "meta": {
                        "decision_reason": f"Downloader reported success but path missing: {resolved_cache_dir}",
                        "search_root": str(model_cache_root),
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    # Copy to final model dir.
    if model_target_path.exists():
        shutil.rmtree(model_target_path)
    shutil.copytree(resolved_cache_dir, model_target_path)

    ok, err = verify_model_dir(model_target_path)
    if not ok:
        extra_json_path.write_text(
            json.dumps(
                {
                    "failure_category": "model",
                    "meta": {
                        "decision_reason": f"Model download completed but verification failed: {err}",
                        "downloader_local_dir": str(resolved_cache_dir),
                        "model_target_dir": str(model_target_path),
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    model_sha = sha256_dir(model_target_path)

    extra_json_path.write_text(
        json.dumps(
            {
                "assets": {
                    "dataset": {
                        "path": str(dataset_path),
                        "source": "generated",
                        "version": "1",
                        "sha256": dataset_sha,
                    },
                    "model": {
                        "path": str(model_target_path),
                        "source": f"hf:{model_id}",
                        "version": revision,
                        "sha256": model_sha,
                    },
                },
                "meta": {
                    "decision_reason": (
                        "Repo provides examples/llama.py (HF Transformers) and no dataset entrypoint; "
                        "prepared a minimal prompts file and downloaded a small public HF model."
                    ),
                    "model_id": model_id,
                    "model_revision": revision,
                    "offline": offline,
                    "downloader_local_dir": str(resolved_cache_dir),
                    **hf_install_meta,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Prepared dataset at {dataset_path}")
    print(f"Prepared model at {model_target_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

python "$repo_root/benchmark_scripts/runner.py" \
  --stage prepare \
  --task download \
  --timeout-sec 1200 \
  --framework pytorch \
  --failure-category download_failed \
  --extra-json "$extra_json" \
  --env "REPO_ROOT=$repo_root" \
  --env "CACHE_DIR=$cache_dir" \
  --env "DATASET_DIR=$dataset_dir" \
  --env "MODEL_DIR=$model_dir" \
  --env "STAGE_DIR=$stage_dir" \
  --env "EXTRA_JSON=$extra_json" \
  --env "HF_HOME=$cache_dir/hf_home" \
  --env "HF_HUB_CACHE=$cache_dir/hf_hub" \
  --env "TRANSFORMERS_CACHE=$cache_dir/transformers" \
  --env "XDG_CACHE_HOME=$cache_dir/xdg" \
  -- \
  "{python}" "$impl_py"
