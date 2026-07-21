#!/usr/bin/env python3
"""Print a staging-invariant SHA-256 identity for the prospective source tree.

The v2 fingerprint binds the current HEAD and every tracked or non-ignored
untracked path as it exists in the working tree. Path, file type, Unix mode, and
content are encoded explicitly. Index-only state is intentionally excluded, so
staging identical bytes cannot invalidate a reviewed snapshot.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DOMAIN = b"tracehelix-source-fingerprint-v2\0"


def git(*args: str) -> bytes:
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["LC_ALL"] = "C"
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.excludesFile=",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.fileMode=true",
            "-C",
            str(ROOT),
            *args,
        ],
        env=env,
        stderr=subprocess.DEVNULL,
    )


def add_field(digest: hashlib._Hash, name: bytes, value: bytes) -> None:
    digest.update(name)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def main() -> None:
    digest = hashlib.sha256(DOMAIN)
    add_field(digest, b"head\0", git("rev-parse", "HEAD").strip())

    paths = sorted(
        set(
            filter(
                None,
                git(
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-per-directory=.gitignore",
                    "-z",
                ).split(b"\0"),
            )
        )
    )
    for raw_path in paths:
        path = ROOT / os.fsdecode(raw_path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            # A tracked path deleted from the prospective working tree contributes
            # no entry, matching the tree that `git add --all` would create.
            continue
        mode = stat.S_IMODE(metadata.st_mode).to_bytes(4, "big")
        if path.is_symlink():
            kind = b"symlink"
            content = os.fsencode(os.readlink(path))
        elif path.is_file():
            kind = b"file"
            content = path.read_bytes()
        else:
            raise RuntimeError(f"Unsupported source path type: {os.fsdecode(raw_path)}")
        add_field(digest, b"path\0", raw_path)
        add_field(digest, b"kind\0", kind)
        add_field(digest, b"mode\0", mode)
        add_field(digest, b"content\0", content)

    print(digest.hexdigest())


if __name__ == "__main__":
    main()
