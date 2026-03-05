#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path, *, encoding: str = "utf-8") -> str:
    return path.read_text(encoding=encoding)


def write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding=encoding)


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tail_lines(path: Path, *, max_lines: int = 220) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return "".join(lines).strip()
        return "".join(lines[-max_lines:]).strip()
    except FileNotFoundError:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files_deterministic(root: Path) -> Iterable[Path]:
    # Deterministic walk: sort directories/files at each level, do not follow symlinks.
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            yield Path(dirpath) / name


def sha256_dir(root: Path) -> str:
    """
    Directory hash based on relative path + file bytes (symlinks are hashed by link target string).
    """
    root = root.resolve()
    h = hashlib.sha256()
    if not root.exists():
        return ""
    for file_path in _iter_files_deterministic(root):
        rel = file_path.relative_to(root).as_posix().encode("utf-8")
        h.update(rel)
        try:
            if file_path.is_symlink():
                h.update(b"\x00SYMLINK\x00")
                h.update(os.readlink(file_path).encode("utf-8"))
            else:
                h.update(b"\x00FILE\x00")
                with file_path.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
        except OSError:
            # Permission/race: include marker so hash is still deterministic given same errors.
            h.update(b"\x00ERROR\x00")
    return h.hexdigest()


def get_git_commit(repo_root: Path = REPO_ROOT) -> Optional[str]:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL, text=True)
        return out.strip() or None
    except Exception:
        return None


def capture_env_vars(keys: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    return out


def is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def run_checked(cmd: List[str], *, cwd: Path, env: Optional[Dict[str, str]] = None, timeout_sec: int = 30) -> Tuple[int, str]:
    try:
        out = subprocess.check_output(cmd, cwd=cwd, env=env, stderr=subprocess.STDOUT, text=True, timeout=timeout_sec)
        return 0, out
    except subprocess.CalledProcessError as e:
        return e.returncode or 1, (e.output or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}\n"


def python_version_string(python_exe: str) -> str:
    code = "import platform; print(platform.python_version())"
    rc, out = run_checked([python_exe, "-c", code], cwd=REPO_ROOT, timeout_sec=30)
    if rc == 0:
        return out.strip()
    return ""

