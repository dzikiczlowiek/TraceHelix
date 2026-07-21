#!/usr/bin/env python3
"""Deterministic TraceHelix source-bundle builder.

Produces a reproducible ``tracehelix-<VERSION>-source.tar.gz`` plus a
``SHA256SUMS`` sidecar from the Git object database of a single commit. The
archive contents and metadata are derived solely from the resolved commit, so
ambient working-tree state (dirty, staged, untracked, or ignored files) can
neither alter nor imply inclusion in the archive.

The archive embeds a canonical ``RELEASE-MANIFEST.json`` describing every
source file. All timestamps, owners, and names are normalized so two builds of
the same commit are byte-identical regardless of host clock, locale, user,
umask, or output path.

CLI:
    python3 scripts/build_release_bundle.py --output-dir /absolute/out [--ref HEAD]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import unicodedata
from dataclasses import dataclass


# --- Hard bounds (testable via module import) -------------------------------

#: Maximum number of tracked source files accepted from a single commit tree.
MAX_SOURCE_FILE_COUNT = 200_000
#: Maximum size of a single Git blob read into memory.
MAX_SINGLE_BLOB_BYTES = 256 * 1024 * 1024
#: Maximum cumulative bytes of all blobs read into memory for one archive.
MAX_TOTAL_BLOB_BYTES = 2 * 1024 * 1024 * 1024

#: Object modes Git can emit in ``ls-tree`` output (regular/exec/symlink/gitlink/tree).
_KNOWN_GIT_MODES = frozenset({0o100644, 0o100755, 0o120000, 0o160000, 0o040000})

#: Canonical SemVer 2.0.0 (permits optional prerelease/build metadata).
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:[.](?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:[+][0-9a-zA-Z-]+(?:[.][0-9a-zA-Z-]+)*)?$"
)

MANIFEST_NAME = "RELEASE-MANIFEST.json"
CHECKSUM_NAME = "SHA256SUMS"

_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_FORBIDDEN_RELEASE_COMPONENTS = frozenset(
    {
        ".hermes",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "bin",
        "obj",
        "dist",
        "test-results",
        "playwright-report",
        "screenshots",
    }
)
_FORBIDDEN_RELEASE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
)


class BuildError(Exception):
    """Raised for any deterministic, user-facing build failure."""


# --- Git access (no shell=True; ambient config neutralized) -----------------


def _git_env() -> dict[str, str]:
    """Return a Git environment free of ambient overrides and locale/timezone."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["TZ"] = "UTC"
    return env


def _run_git(args: list[str], cwd: Path) -> bytes:
    """Run a Git subcommand and return stdout bytes; raise BuildError on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise BuildError(f"git {' '.join(args)} failed: {message}")
    return proc.stdout


def discover_repo_root(start: Path) -> Path:
    """Return the worktree root containing ``start``."""
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=start).decode("utf-8").strip()
    if not out:
        raise BuildError("could not determine repository top-level")
    return Path(out)


def resolve_ref(repo_root: Path, ref: str) -> str:
    """Resolve ``ref`` to an exact 40-hex commit object id."""
    if not ref:
        raise BuildError("ref must be a non-empty string")
    out = _run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repo_root,
    )
    commit = out.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BuildError(f"resolved ref is not a 40-hex commit: {commit!r}")
    return commit


# --- Tree parsing & validation ---------------------------------------------


@dataclass(frozen=True)
class TreeEntry:
    """A single entry parsed from ``git ls-tree`` output."""

    mode: int
    obj_type: str
    sha: str
    path: str


def parse_ls_tree_record(record: bytes) -> TreeEntry:
    """Parse one NUL-free ``git ls-tree`` record into a validated TreeEntry.

    Rejects malformed records and object modes Git cannot produce (used directly
    by unit tests to exercise unsupported-mode rejection without a real repo).
    """
    if not record:
        raise BuildError("empty ls-tree record")
    if b"\t" not in record:
        raise BuildError(f"malformed ls-tree record (no path): {record!r}")
    meta_b, path_b = record.split(b"\t", 1)
    parts = meta_b.split(b" ")
    if len(parts) != 3:
        raise BuildError(f"malformed ls-tree record (meta): {record!r}")
    mode_b, type_b, sha_b = parts
    try:
        mode = int(mode_b.decode("ascii"), 8)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BuildError(f"unsupported object mode {mode_b!r}") from exc
    if mode not in _KNOWN_GIT_MODES:
        raise BuildError(f"unsupported object mode {mode_b!r}")
    try:
        obj_type = type_b.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BuildError(f"unsupported object type {type_b!r}") from exc
    if obj_type not in ("blob", "commit", "tree"):
        raise BuildError(f"unsupported object type {obj_type!r}")
    try:
        sha = sha_b.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BuildError(f"unsupported object id {sha_b!r}") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BuildError(f"invalid object id {sha!r}")
    try:
        path = path_b.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"non-UTF-8 path in tree: {path_b!r}") from exc
    return TreeEntry(mode=mode, obj_type=obj_type, sha=sha, path=path)


def validate_source_path(path: str) -> None:
    """Validate a single relative Git path for safe archive inclusion.

    Rejects empty paths, absolute paths, traversal (``..``), self (``.``),
    empty components, backslashes, C0/DEL controls, and names whose extraction
    is ambiguous or invalid on common Windows filesystems.
    """
    if not path:
        raise BuildError("empty source path")
    if "\x00" in path:
        raise BuildError(f"NUL in source path: {path!r}")
    if "\\" in path:
        raise BuildError(f"backslash in source path: {path!r}")
    for ch in path:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise BuildError(f"control character in source path: {path!r}")
    if path.startswith("/"):
        raise BuildError(f"absolute source path: {path!r}")
    for component in path.split("/"):
        if component == "":
            raise BuildError(f"empty path component: {path!r}")
        if component == ".":
            raise BuildError(f"self-referential path component: {path!r}")
        if component == "..":
            raise BuildError(f"traversal path component: {path!r}")
        if component.endswith((" ", ".")):
            raise BuildError(f"platform-ambiguous trailing dot/space: {path!r}")
        if any(char in _WINDOWS_FORBIDDEN_CHARS for char in component):
            raise BuildError(f"platform-invalid character in source path: {path!r}")
        stem = component.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_STEMS:
            raise BuildError(f"platform-reserved source path: {path!r}")


def archive_mode_for(mode: int) -> int:
    """Map a supported regular-file Git mode to its normalized archive mode."""
    if mode == 0o100644:
        return 0o644
    if mode == 0o100755:
        return 0o755
    raise BuildError(f"mode is not a regular file: {mode:o}")


def is_release_source_path(path: str) -> bool:
    """Return whether a validated tracked path belongs in the source release."""
    components = path.split("/")
    folded_components = tuple(component.casefold() for component in components)
    if any(component in _FORBIDDEN_RELEASE_COMPONENTS for component in folded_components):
        return False
    folded_path = path.casefold()
    if folded_components[0] == "traces" or folded_components[:2] == ("web", "traces"):
        return False
    if folded_path.endswith(_FORBIDDEN_RELEASE_SUFFIXES):
        return False
    if folded_components[0] == "imports" and folded_path != "imports/.gitkeep":
        return False
    name = folded_components[-1]
    if name == ".env" or name.startswith(".env."):
        return False
    return True


def read_tree(repo_root: Path, commit: str) -> list[TreeEntry]:
    """Read every entry of ``commit``'s tree from the Git object database."""
    raw = _run_git(["ls-tree", "-rz", "--full-tree", commit], cwd=repo_root)
    records = [chunk for chunk in raw.split(b"\x00") if chunk]
    entries: list[TreeEntry] = []
    for record in records:
        entries.append(parse_ls_tree_record(record))
    return entries


def select_source_files(entries: list[TreeEntry]) -> list[TreeEntry]:
    """Validate entries and return the regular-file blobs to archive.

    Rejects symlinks, submodules, trees, unsupported modes, unsafe paths,
    duplicate paths, and Unicode casefold collisions. Tracked development,
    cache, test-output, local-database, and private-import paths are omitted.
    """
    if len(entries) > MAX_SOURCE_FILE_COUNT:
        raise BuildError(
            f"source file count {len(entries)} exceeds limit {MAX_SOURCE_FILE_COUNT}"
        )
    seen_exact: set[str] = set()
    seen_casefold: dict[str, str] = {}
    files: list[TreeEntry] = []
    for entry in entries:
        validate_source_path(entry.path)
        if entry.path in seen_exact:
            raise BuildError(f"duplicate source path: {entry.path!r}")
        seen_exact.add(entry.path)
        # NFC + casefold models the common extraction collision boundary across
        # case-insensitive and Unicode-normalizing filesystems.
        folded = unicodedata.normalize("NFC", entry.path).casefold()
        if folded in seen_casefold:
            raise BuildError(
                f"Unicode casefold collision: {entry.path!r} vs {seen_casefold[folded]!r}"
            )
        seen_casefold[folded] = entry.path
        if entry.obj_type == "commit":
            raise BuildError(f"submodule (gitlink) not supported: {entry.path!r}")
        if entry.obj_type == "tree":
            raise BuildError(f"tree entry not supported: {entry.path!r}")
        if entry.mode == 0o120000:
            raise BuildError(f"symlink not supported: {entry.path!r}")
        if entry.mode == 0o160000:
            raise BuildError(f"submodule (gitlink) not supported: {entry.path!r}")
        if entry.mode in (0o100644, 0o100755):
            if is_release_source_path(entry.path):
                files.append(entry)
        else:
            raise BuildError(f"unsupported object mode {entry.mode:o} for {entry.path!r}")
    return files


# --- Blob reading with bounds ----------------------------------------------


def read_blobs(repo_root: Path, shas: list[str]) -> dict[str, bytes]:
    """Read unique blob contents via a single ``git cat-file --batch`` stream."""
    unique = list(dict.fromkeys(shas))
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=str(repo_root),
        env=_git_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    if proc.stdin is None or proc.stdout is None:
        raise BuildError("could not open git cat-file stream")
    out = proc.stdout
    contents: dict[str, bytes] = {}
    total = 0
    try:
        proc.stdin.write("".join(f"{sha}\n" for sha in unique).encode("ascii"))
        proc.stdin.close()
        for sha in unique:
            header = out.readline()
            if not header:
                raise BuildError(f"git cat-file produced no header for {sha}")
            tokens = header.rstrip(b"\n").split(b" ")
            if len(tokens) == 2 and tokens[1] == b"missing":
                raise BuildError(f"git object missing: {tokens[0].decode('ascii', 'replace')}")
            if len(tokens) != 3:
                raise BuildError(f"git cat-file malformed header: {header!r}")
            obj_sha_b, obj_type_b, size_b = tokens
            obj_type = obj_type_b.decode("ascii", "replace")
            if obj_type != "blob":
                raise BuildError(f"object {sha} is not a blob (type {obj_type!r})")
            try:
                size = int(size_b)
            except ValueError as exc:
                raise BuildError(f"git cat-file malformed size: {size_b!r}") from exc
            if size < 0:
                raise BuildError(f"negative blob size for {sha}")
            if size > MAX_SINGLE_BLOB_BYTES:
                raise BuildError(
                    f"blob {sha} size {size} exceeds single-blob limit {MAX_SINGLE_BLOB_BYTES}"
                )
            total += size
            if total > MAX_TOTAL_BLOB_BYTES:
                raise BuildError(
                    f"total blob bytes {total} exceed limit {MAX_TOTAL_BLOB_BYTES}"
                )
            data = out.read(size)
            if len(data) != size:
                raise BuildError(f"git cat-file short read for {sha}")
            trailing = out.read(1)
            if trailing != b"\n":
                raise BuildError(f"git cat-file missing record separator for {sha}")
            contents[sha] = data
    finally:
        out.close()
        proc.wait()
    if proc.returncode not in (0, None):
        raise BuildError(f"git cat-file exited with {proc.returncode}")
    return contents


# --- Manifest & archive construction ---------------------------------------


def build_manifest(
    version: str,
    commit: str,
    archive_root: str,
    files: list[tuple[str, int, bytes]],
) -> bytes:
    """Build canonical UTF-8 ``RELEASE-MANIFEST.json`` bytes (no source entry for it)."""
    entries = [
        {
            "path": path,
            "mode": f"{mode:04o}",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, mode, content in files
    ]
    manifest = {
        "schemaVersion": 1,
        "version": version,
        "commit": commit,
        "archiveRoot": archive_root,
        "timestampPolicy": "unix-epoch",
        "entries": entries,
    }
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return encoded + b"\n"


def build_tar(
    archive_root: str,
    files: list[tuple[str, int, bytes]],
) -> bytes:
    """Build deterministic USTAR/PAX-safe (uncompressed) tar bytes.

    Entries are emitted in bytewise-sorted archive-path order, with explicit
    directory entries for the root and every ancestor of each file.
    """
    dir_names: set[str] = {archive_root}
    for relpath, _, _ in files:
        current = archive_root
        for component in relpath.split("/")[:-1]:
            current = f"{current}/{component}"
            dir_names.add(current)

    items: list[tuple[str, bool, int, bytes]] = []
    for name in dir_names:
        # DIRTYPE carries the directory semantics; omitting a trailing slash
        # keeps the serialized member names in the same strict bytewise order
        # that readers expose (important for file/dir prefixes such as api.ts
        # and api/index.txt).
        items.append((name, True, 0o755, b""))
    for relpath, mode, content in files:
        items.append((f"{archive_root}/{relpath}", False, mode, content))
    items.sort(key=lambda item: item[0].encode("utf-8"))

    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer,
        mode="w",
        format=tarfile.PAX_FORMAT,
        encoding="utf-8",
        errors="strict",
    ) as tar:
        for name, is_dir, mode, content in items:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.size = 0
                tar.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def gzip_deterministic(data: bytes) -> bytes:
    """Gzip ``data`` with mtime 0, empty filename, fixed compression level."""
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=buffer,
        mtime=0,
        compresslevel=9,
    ) as compressor:
        compressor.write(data)
    return buffer.getvalue()


# --- Output handling --------------------------------------------------------


def prepare_output_dir(output_dir_arg: str, repo_root: Path) -> Path:
    """Validate and return the resolved output directory.

    The repository-containment check runs against the resolved path *before* any
    filesystem creation so a rejected inside-repo output leaves no partial dir.
    """
    raw = Path(output_dir_arg)
    if not raw.is_absolute():
        raise BuildError("--output-dir must be an absolute path")

    real_root = repo_root.resolve()
    candidate = raw.resolve(strict=False)
    if candidate == real_root or real_root in candidate.parents:
        raise BuildError("--output-dir must be outside the repository worktree")

    try:
        raw.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise BuildError(f"--output-dir exists and is not a directory: {raw}") from exc
    except OSError as exc:
        raise BuildError(f"--output-dir could not be created: {raw}: {exc}") from exc
    if raw.is_symlink():
        raise BuildError("--output-dir must not be a symlink")
    if not raw.is_dir():
        raise BuildError(f"--output-dir is not a directory: {raw}")

    real = raw.resolve()
    if real == real_root or real_root in real.parents:
        raise BuildError("--output-dir must not resolve inside the repository worktree")
    return real


def write_outputs(
    output_dir: Path,
    archive_name: str,
    archive_bytes: bytes,
    checksum_bytes: bytes,
) -> tuple[Path, Path]:
    """Atomically write the archive and checksum; never leave partial artifacts."""
    archive_final = output_dir / archive_name
    checksum_final = output_dir / CHECKSUM_NAME
    if archive_final.exists() or checksum_final.exists():
        raise BuildError(
            "output archive or checksum already exists; refusing to overwrite"
        )

    suffix = f".{os.getpid()}.{os.urandom(8).hex()}"
    archive_tmp = output_dir / f".{archive_name}.tmp{suffix}"
    checksum_tmp = output_dir / f".{CHECKSUM_NAME}.tmp{suffix}"
    archive_renamed = False
    checksum_renamed = False
    try:
        _write_exclusive(archive_tmp, archive_bytes)
        _write_exclusive(checksum_tmp, checksum_bytes)
        _rename_no_clobber(archive_tmp, archive_final)
        archive_tmp = None  # type: ignore[assignment]
        archive_renamed = True
        _rename_no_clobber(checksum_tmp, checksum_final)
        checksum_tmp = None  # type: ignore[assignment]
        checksum_renamed = True
    except BaseException:
        for stale in (archive_tmp, checksum_tmp):
            if stale is not None:
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        if archive_renamed or checksum_renamed:
            # Remove only finals created by this invocation. A no-clobber
            # failure may mean a concurrent writer owns the other final.
            owned_finals = []
            if archive_renamed:
                owned_finals.append(archive_final)
            if checksum_renamed:
                owned_finals.append(checksum_final)
            for final in owned_finals:
                try:
                    final.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        raise
    return archive_final, checksum_final


def _rename_no_clobber(source: Path, destination: Path) -> None:
    """Publish ``source`` at ``destination`` without ever replacing a racer.

    Both paths live in the same output directory. A hard link provides atomic
    create-if-absent semantics on the supported local filesystems; only after
    it succeeds is the temporary name removed.
    """
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise BuildError(f"output already exists; refusing to overwrite: {destination}") from exc
    except OSError as exc:
        raise BuildError(f"could not publish output {destination}: {exc}") from exc

    try:
        source.unlink()
    except OSError as exc:
        # Roll back the final we just created. Never leave two names or claim
        # successful publication when temporary cleanup failed.
        try:
            destination.unlink()
        except OSError:
            pass
        raise BuildError(f"could not remove temporary output {source}: {exc}") from exc


def _write_exclusive(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively, write, and fsync before returning."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


# --- Orchestration ----------------------------------------------------------


def build(output_dir_arg: str, ref: str) -> dict[str, object]:
    """Run the full build and return the JSON summary dictionary."""
    repo_root = discover_repo_root(Path.cwd())
    commit = resolve_ref(repo_root, ref)

    entries = read_tree(repo_root, commit)
    source_entries = select_source_files(entries)
    source_entries.sort(key=lambda entry: entry.path)

    version_entry = next(
        (entry for entry in source_entries if entry.path == "VERSION"),
        None,
    )
    if version_entry is None:
        raise BuildError("VERSION file not present at repository root")
    version_blobs = read_blobs(repo_root, [version_entry.sha])
    version = version_blobs[version_entry.sha].decode("utf-8").strip()
    if SEMVER_PATTERN.fullmatch(version) is None:
        raise BuildError(f"VERSION is not canonical SemVer: {version!r}")

    archive_root = f"tracehelix-{version}"
    archive_name = f"{archive_root}-source.tar.gz"

    blobs = read_blobs(repo_root, [entry.sha for entry in source_entries])
    files: list[tuple[str, int, bytes]] = [
        (entry.path, archive_mode_for(entry.mode), blobs[entry.sha])
        for entry in source_entries
    ]

    manifest_bytes = build_manifest(version, commit, archive_root, files)
    manifest_entry = (MANIFEST_NAME, 0o644, manifest_bytes)

    tar_bytes = build_tar(archive_root, files + [manifest_entry])
    archive_bytes = gzip_deterministic(tar_bytes)

    sha_hex = hashlib.sha256(archive_bytes).hexdigest()
    checksum_bytes = f"{sha_hex}  {archive_name}\n".encode("ascii")

    output_dir = prepare_output_dir(output_dir_arg, repo_root)
    archive_path, checksum_path = write_outputs(
        output_dir, archive_name, archive_bytes, checksum_bytes
    )

    return {
        "archive": str(archive_path),
        "checksum": str(checksum_path),
        "version": version,
        "commit": commit,
        "sha256": sha_hex,
        "sourceFileCount": len(source_entries),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_release_bundle.py",
        description="Build a deterministic TraceHelix source bundle from a Git commit.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Absolute path to an output directory outside the repository worktree.",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="Git ref to resolve to an exact commit (default: HEAD).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = build(args.output_dir, args.ref)
    except BuildError as exc:
        print(f"build_release_bundle: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI must not dump to stdout
        print(f"build_release_bundle: unexpected error: {exc}", file=sys.stderr)
        return 1
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
