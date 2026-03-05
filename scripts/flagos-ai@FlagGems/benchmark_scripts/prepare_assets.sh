#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model/tokenizer download).

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Notes:
  - Uses the agent report python by default (see /opt/scimlopsbench/report.json).
  - Downloads HF assets into benchmark_assets/cache/ and links/copies into benchmark_assets/model/.

Optional:
  --python <path>        Override python interpreter (highest priority)
  --report-path <path>   Override report.json path
EOF
}

python_override=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"

# Generate a small python implementation into build_output (write-only allowed).
impl_py="$stage_dir/_prepare_assets_impl.py"
assets_json="$stage_dir/assets.json"
meta_json="$stage_dir/meta_extra.json"

cat >"$impl_py" <<'PY'
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path


EXIT_AUTH_REQUIRED = 11
EXIT_DOWNLOAD_FAILED = 12
EXIT_DEPS = 13
EXIT_DATA = 14
EXIT_MODEL = 15


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dir(path: Path) -> str:
    h = hashlib.sha256()
    files = [p for p in path.rglob("*") if p.is_file()]
    for p in sorted(files, key=lambda x: str(x.relative_to(path))):
        rel = str(p.relative_to(path)).encode("utf-8")
        h.update(rel + b"\0")
        h.update(sha256_file(p).encode("utf-8") + b"\0")
    return h.hexdigest()


def ensure_pkg(mod: str, pip_name: str, install_attempts: list[dict]) -> None:
    try:
        __import__(mod)
        return
    except Exception:
        pass

    cmd = [sys.executable, "-m", "pip", "install", "-q", pip_name]
    install_attempts.append({"module": mod, "pip": pip_name, "command": " ".join(cmd)})
    try:
        import subprocess

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        install_attempts[-1]["returncode"] = p.returncode
        install_attempts[-1]["output_tail"] = "\n".join((p.stdout or "").splitlines()[-60:])
        if p.returncode != 0:
            raise RuntimeError(f"pip install failed: {pip_name}")
    except Exception as e:
        raise RuntimeError(f"deps_install_failed:{pip_name}:{e}") from e


def main() -> int:
    repo_root = Path(os.environ.get("SCIMLOPSBENCH_REPO_ROOT", Path(__file__).resolve().parents[2]))
    assets_root = repo_root / "benchmark_assets"
    cache_root = assets_root / "cache"
    dataset_root = assets_root / "dataset"
    model_root = assets_root / "model"

    stage_dir = repo_root / "build_output" / "prepare"
    assets_json_path = stage_dir / "assets.json"
    meta_json_path = stage_dir / "meta_extra.json"

    for p in (cache_root, dataset_root, model_root, stage_dir):
        p.mkdir(parents=True, exist_ok=True)

    install_attempts: list[dict] = []

    # Ensure minimal deps for downloading HF tokenizer.
    try:
        ensure_pkg("huggingface_hub", "huggingface_hub", install_attempts)
    except Exception as e:
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": str(e)}, indent=2),
            encoding="utf-8",
        )
        return EXIT_DEPS

    # transformers isn't required for snapshot_download, but is required for the single-GPU entrypoint we use.
    try:
        ensure_pkg("transformers", "transformers", install_attempts)
    except Exception as e:
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": str(e)}, indent=2),
            encoding="utf-8",
        )
        return EXIT_DEPS

    # Dataset: a minimal prompts file mirroring the repo's BERT example.
    dataset_cache_dir = cache_root / "dataset"
    dataset_cache_dir.mkdir(parents=True, exist_ok=True)
    dataset_cache_path = dataset_cache_dir / "prompts.jsonl"
    dataset_path = dataset_root / "prompts.jsonl"
    try:
        dataset_lines = [{"text": "How are you today?"}]
        dataset_cache_path.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in dataset_lines) + "\n",
            encoding="utf-8",
        )
        dataset_sha = sha256_file(dataset_cache_path)
        if dataset_path.exists() and sha256_file(dataset_path) == dataset_sha:
            pass
        else:
            shutil.copy2(dataset_cache_path, dataset_path)
    except Exception as e:
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": f"dataset_write_failed:{e}"}, indent=2),
            encoding="utf-8",
        )
        return EXIT_DATA

    # Model/tokenizer: use the repo's example tokenizer id.
    model_id = "google-bert/bert-base-uncased"
    os.environ.setdefault("HF_HOME", str(cache_root / "hf_home"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import HfHubHTTPError

    allow_patterns = [
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.txt",
        "added_tokens.json",
        "merges.txt",
        "config.json",
    ]

    snapshot_path: Path
    revision = os.environ.get("HF_REVISION", "main")
    try:
        snapshot_path = Path(
            snapshot_download(
                repo_id=model_id,
                revision=revision,
                allow_patterns=allow_patterns,
            )
        )
    except HfHubHTTPError as e:
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": f"hf_http_error:{e}"}, indent=2),
            encoding="utf-8",
        )
        # auth required for 401/403
        if "401" in str(e) or "403" in str(e):
            return EXIT_AUTH_REQUIRED
        return EXIT_DOWNLOAD_FAILED
    except Exception as e:
        # Try offline reuse.
        try:
            snapshot_path = Path(
                snapshot_download(
                    repo_id=model_id,
                    revision=revision,
                    allow_patterns=allow_patterns,
                    local_files_only=True,
                )
            )
        except Exception as e2:
            meta_json_path.write_text(
                json.dumps({"install_attempts": install_attempts, "error": f"hf_download_failed:{e}; offline_failed:{e2}"}, indent=2),
                encoding="utf-8",
            )
            return EXIT_DOWNLOAD_FAILED

    if not snapshot_path.exists():
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": f"snapshot_path_missing:{snapshot_path}"}, indent=2),
            encoding="utf-8",
        )
        return EXIT_MODEL

    # Link/copy into benchmark_assets/model.
    resolved_model_dir = model_root / "google-bert__bert-base-uncased__tokenizer"
    try:
        if resolved_model_dir.exists() or resolved_model_dir.is_symlink():
            # Keep existing content if present.
            pass
        else:
            try:
                resolved_model_dir.symlink_to(snapshot_path, target_is_directory=True)
            except Exception:
                shutil.copytree(snapshot_path, resolved_model_dir)
    except Exception as e:
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": f"model_link_or_copy_failed:{e}", "snapshot_path": str(snapshot_path)}, indent=2),
            encoding="utf-8",
        )
        return EXIT_MODEL

    # Verify resolved model dir exists and compute sha.
    try:
        model_sha = sha256_dir(resolved_model_dir.resolve())
    except Exception as e:
        meta_json_path.write_text(
            json.dumps({"install_attempts": install_attempts, "error": f"model_sha_failed:{e}", "resolved_model_dir": str(resolved_model_dir)}, indent=2),
            encoding="utf-8",
        )
        return EXIT_MODEL

    assets = {
        "dataset": {
            "path": str(dataset_path),
            "source": "generated:prompts.jsonl",
            "version": "v1",
            "sha256": dataset_sha,
        },
        "model": {
            "path": str(resolved_model_dir),
            "source": f"hf://{model_id}",
            "version": revision,
            "sha256": model_sha,
        },
    }
    assets_json_path.write_text(json.dumps(assets, indent=2, ensure_ascii=False), encoding="utf-8")

    meta_extra = {
        "install_attempts": install_attempts,
        "host": {"python": sys.executable, "python_version": platform.python_version()},
    }
    try:
        import transformers  # noqa: F401

        meta_extra["transformers_version"] = getattr(transformers, "__version__", "")
    except Exception:
        meta_extra["transformers_version"] = ""

    meta_json_path.write_text(json.dumps(meta_extra, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Prepared dataset at {dataset_path}")
    print(f"Prepared model/tokenizer at {resolved_model_dir} -> {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

runner_py="$repo_root/benchmark_scripts/runner.py"
pybin="python3"
command -v python3 >/dev/null 2>&1 || pybin="python"

runner_args=(
  "--stage" "prepare"
  "--task" "download"
  "--framework" "pytorch"
  "--timeout-sec" "1200"
  "--out-dir" "$stage_dir"
  "--decision-reason" "Use repo example tokenizer (examples/model_bert_test.py) and generated prompt dataset."
  "--env" "PIP_CACHE_DIR=$repo_root/benchmark_assets/cache/pip"
  "--env" "XDG_CACHE_HOME=$repo_root/benchmark_assets/cache/xdg"
  "--env" "HF_HOME=$repo_root/benchmark_assets/cache/hf_home"
  "--env" "HF_HUB_DISABLE_TELEMETRY=1"
  "--assets-json-path" "$assets_json"
  "--extra-meta-json-path" "$meta_json"
)

if [[ -n "$report_path" ]]; then
  runner_args+=("--report-path" "$report_path")
fi
if [[ -n "$python_override" ]]; then
  runner_args+=("--python" "$python_override")
else
  runner_args+=("--python-required")
fi

"$pybin" "$runner_py" "${runner_args[@]}" -- "{python}" "$impl_py"
