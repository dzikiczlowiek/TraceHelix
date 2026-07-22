#!/usr/bin/env python3
"""Canonical, fail-closed release-asset assembly, verification, and export."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
from typing import NoReturn

import verify_release_bundle as bundle


class ReleaseAssetError(Exception):
    """A release asset or publication precondition is invalid."""


class ExportInterrupted(Exception):
    """An export received a termination signal and was rolled back."""


def asset_names(version: str) -> tuple[str, ...]:
    """Return the one canonical ordered seven-file public asset specification."""
    return (
        f"tracehelix-{version}-source.tar.gz",
        "SHA256SUMS",
        "RELEASE-MANIFEST.json",
        "RELEASE-NOTES.md",
        f"tracehelix-{version}-source.cdx.json",
        "tracehelix-api.spdx.json",
        "tracehelix-web.spdx.json",
    )


def _fail(message: str) -> NoReturn:
    raise ReleaseAssetError(message)


def _regular_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(f"cannot stat {label}: {exc}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"{label} must be a regular non-symlink file")


def require_exact_flat_assets(directory: Path, version: str) -> dict[str, Path]:
    """Require exactly the canonical files directly inside a real directory."""
    try:
        metadata = directory.lstat()
    except OSError as exc:
        _fail(f"cannot stat asset directory: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("asset directory must be a real directory, not a symlink")
    expected = asset_names(version)
    found: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                # lstat rejects symlinks, directories, sockets, and FIFOs before
                # any content is opened; nested content is therefore rejected too.
                metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    _fail(f"asset handoff contains non-regular entry: {entry.name}")
                found.append(entry.name)
    except OSError as exc:
        _fail(f"cannot enumerate asset directory: {exc}")
    if set(found) != set(expected) or len(found) != len(expected):
        _fail(f"asset handoff must contain exactly: {', '.join(expected)}")
    paths = {name: directory / name for name in expected}
    for name, path in paths.items():
        _regular_file(path, f"asset {name}")
    return paths


def verify_source_assets(paths: dict[str, Path], version: str) -> None:
    """Verify the archive checksum and require its manifest bytes externally."""
    archive_name = asset_names(version)[0]
    archive = paths[archive_name]
    checksums = paths["SHA256SUMS"]
    try:
        raw, _ = bundle.verify_checksum(archive, checksums)
        manifest, payloads = bundle.inspect_archive(bundle.decompress_strict(raw), archive.name)
    except (bundle.VerifyError, OSError) as exc:
        _fail(f"source bundle verification failed: {exc}")
    if manifest.get("version") != version:
        _fail("source manifest version does not match VERSION")
    embedded_name = f"tracehelix-{version}/{bundle.MANIFEST_NAME}"
    embedded = [data for member, data in payloads if member.name == embedded_name]
    if len(embedded) != 1:
        _fail("source archive has no unique embedded release manifest")
    try:
        external = paths["RELEASE-MANIFEST.json"].read_bytes()
    except OSError as exc:
        _fail(f"cannot read external release manifest: {exc}")
    if external != embedded[0]:
        _fail("external RELEASE-MANIFEST.json does not exactly match source archive")


def handoff_digest(paths: dict[str, Path], version: str) -> str:
    """Hash canonical filename/length/bytes records without delimiter ambiguity."""
    digest = hashlib.sha256()
    for name in asset_names(version):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        try:
            size = paths[name].stat().st_size
            digest.update(size.to_bytes(8, "big"))
            with paths[name].open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            _fail(f"cannot read asset {name}: {exc}")
    return digest.hexdigest()


def verify_handoff(directory: Path, version: str) -> str:
    paths = require_exact_flat_assets(directory, version)
    verify_source_assets(paths, version)
    return handoff_digest(paths, version)


def assemble(directory: Path, version: str) -> str:
    return verify_handoff(directory, version)


def verify_publish(directory: Path, version: str, tag: str, expected_digest: str) -> str:
    if tag != f"v{version}":
        _fail(f"tag {tag!r} does not equal v{version}")
    if len(expected_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_digest):
        _fail("expected handoff digest must be lowercase SHA-256")
    actual = verify_handoff(directory, version)
    if actual != expected_digest:
        _fail("downloaded release assets do not match assemble handoff digest")
    return actual


def assert_release_absent(tag: str, gh: str = "gh") -> None:
    """Proceed only on an authoritative GitHub API 404 for the release tag."""
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        _fail("GITHUB_REPOSITORY is required to check release absence")
    result = subprocess.run(
        [gh, "api", "--include", f"repos/{repository}/releases/tags/{tag}"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        _fail(f"release {tag} already exists; refusing to overwrite")
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if not re.fullmatch(r"HTTP/\S+ 404(?: Not Found)?", first_line):
        _fail(
            "could not authoritatively establish release absence: "
            + (first_line or result.stderr.strip() or f"gh exited {result.returncode}")
        )


def create_release(
    directory: Path,
    version: str,
    tag: str,
    expected_digest: str,
    gh: str = "gh",
) -> None:
    """Reverify the bound handoff, then run the fixed release argv."""
    verify_publish(directory, version, tag, expected_digest)
    paths = require_exact_flat_assets(directory, version)
    argv = [
        gh,
        "release",
        "create",
        tag,
        "--verify-tag",
        "--title",
        f"TraceHelix {version}",
        "--notes-file",
        str(paths["RELEASE-NOTES.md"]),
        *(str(paths[name]) for name in asset_names(version)),
    ]
    subprocess.run(argv, check=True)


def _open_regular_source(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        _fail(f"export source must be a regular file: {path}")
    return fd


def export_verified(
    archive: Path,
    checksums: Path,
    manifest: Path,
    version: str,
    destination: Path,
    forbidden_roots: tuple[Path, ...] = (),
) -> None:
    """Verify then exclusively export the archive, checksum, and manifest.

    The destination is opened once by descriptor so an attacker cannot replace
    its path between the empty check and an O_EXCL create. Any owned partial
    output is removed on errors or TERM/INT before the exception escapes.
    """
    archive_name = asset_names(version)[0]
    paths = {
        archive_name: archive,
        "SHA256SUMS": checksums,
        "RELEASE-MANIFEST.json": manifest,
    }
    for name, path in paths.items():
        _regular_file(path, f"export source {name}")
    verify_source_assets(paths, version)
    try:
        metadata = destination.lstat()
    except OSError as exc:
        _fail(f"cannot stat export destination: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("export destination must be a pre-existing real directory")
    try:
        destination = destination.resolve(strict=True)
        canonical_roots = tuple(root.resolve(strict=True) for root in forbidden_roots)
    except OSError as exc:
        _fail(f"cannot canonicalize export boundary: {exc}")
    for root in canonical_roots:
        if destination == root or root in destination.parents:
            _fail(f"export destination must be outside forbidden root: {root}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    destination_fd = os.open(destination, flags)
    created: list[tuple[str, int, int]] = []
    previous_handlers: dict[int, object] = {}

    def interrupted(_signum: int, _frame: object) -> None:
        raise ExportInterrupted("export interrupted")

    try:
        if os.listdir(destination_fd):
            _fail("export destination must be empty")
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        selected = (asset_names(version)[0], "SHA256SUMS", "RELEASE-MANIFEST.json")
        for name in selected:
            source_fd = destination_file_fd = None
            try:
                source_fd = _open_regular_source(paths[name])
                create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    create_flags |= os.O_NOFOLLOW
                destination_file_fd = os.open(name, create_flags, 0o644, dir_fd=destination_fd)
                owned = os.fstat(destination_file_fd)
                created.append((name, owned.st_dev, owned.st_ino))
                while chunk := os.read(source_fd, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(destination_file_fd, view):]
                os.fsync(destination_file_fd)
            finally:
                if source_fd is not None:
                    os.close(source_fd)
                if destination_file_fd is not None:
                    os.close(destination_file_fd)
    except BaseException:
        for name, device, inode in reversed(created):
            try:
                current = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if current.st_dev == device and current.st_ino == inode:
                try:
                    os.unlink(name, dir_fd=destination_fd)
                except FileNotFoundError:
                    pass
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        os.close(destination_fd)


def _write_output(path: str, name: str, value: str) -> None:
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("assemble", "verify-publish", "create-release"):
        command = sub.add_parser(name)
        command.add_argument("--directory", required=True)
        command.add_argument("--version", required=True)
    export_parser = sub.add_parser("export-verified")
    export_parser.add_argument("--archive", required=True)
    export_parser.add_argument("--checksums", required=True)
    export_parser.add_argument("--manifest", required=True)
    export_parser.add_argument("--version", required=True)
    export_parser.add_argument("--destination", required=True)
    export_parser.add_argument("--forbidden-root", action="append", default=[])
    assemble_parser = sub.choices["assemble"]
    assemble_parser.add_argument("--github-output")
    publish_parser = sub.choices["verify-publish"]
    publish_parser.add_argument("--tag", required=True)
    publish_parser.add_argument("--expected-digest", required=True)
    create_parser = sub.choices["create-release"]
    create_parser.add_argument("--tag", required=True)
    create_parser.add_argument("--expected-digest", required=True)
    create_parser.add_argument("--gh", default="gh")
    absent = sub.add_parser("assert-release-absent")
    absent.add_argument("--tag", required=True)
    absent.add_argument("--gh", default="gh")
    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            digest = assemble(Path(args.directory), args.version)
            if args.github_output:
                _write_output(args.github_output, "handoff_digest", digest)
            print(digest)
        elif args.command == "verify-publish":
            print(verify_publish(Path(args.directory), args.version, args.tag, args.expected_digest))
        elif args.command == "export-verified":
            export_verified(
                Path(args.archive),
                Path(args.checksums),
                Path(args.manifest),
                args.version,
                Path(args.destination),
                tuple(Path(root) for root in args.forbidden_root),
            )
        elif args.command == "create-release":
            create_release(
                Path(args.directory),
                args.version,
                args.tag,
                args.expected_digest,
                args.gh,
            )
        else:
            assert_release_absent(args.tag, args.gh)
    except (ReleaseAssetError, ExportInterrupted, OSError, subprocess.CalledProcessError) as exc:
        print(f"release_assets: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
