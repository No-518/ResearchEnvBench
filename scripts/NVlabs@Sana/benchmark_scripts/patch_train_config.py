#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Tuple


def _split_newline(line: str) -> Tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def patch_resume_from_empty_mapping(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out = list(lines)

    key_re = re.compile(r"^(?P<indent>\s*)resume_from:(?P<rest>.*)$")

    for i, line in enumerate(lines):
        m = key_re.match(line)
        if not m:
            continue

        indent = m.group("indent")
        raw, newline = _split_newline(line)
        rest = raw[len(indent) + len("resume_from:") :]

        value_part = rest
        comment_part = ""
        hash_idx = value_part.find("#")
        if hash_idx != -1:
            comment_part = value_part[hash_idx:].strip()
            value_part = value_part[:hash_idx]

        value = value_part.strip()
        if value and value.lower() not in ("null", "~", "none"):
            continue

        # If resume_from has nested keys, keep it as-is.
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt or nxt.startswith("#"):
                j += 1
                continue
            next_indent = re.match(r"^(\s*)", lines[j]).group(1)
            if len(next_indent) > len(indent):
                # nested mapping present
                break

            new_line = f"{indent}resume_from: latest"
            if comment_part:
                new_line += " " + comment_part
            out[i] = new_line + newline
            break

    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Patch Sana YAML config for env-bench compatibility.")
    ap.add_argument("input_yaml", type=Path)
    ap.add_argument("output_yaml", type=Path)
    args = ap.parse_args(argv)

    src = args.input_yaml
    dst = args.output_yaml

    try:
        text = src.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[patch_train_config] failed to read {src}: {e}", file=sys.stderr)
        return 1

    patched = patch_resume_from_empty_mapping(text)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(patched, encoding="utf-8")
    except Exception as e:
        print(f"[patch_train_config] failed to write {dst}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
