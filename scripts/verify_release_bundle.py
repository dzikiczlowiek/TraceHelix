#!/usr/bin/env python3
"""Fail-closed verification and safe extraction of TraceHelix source bundles."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import unicodedata
import zlib

MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
MAX_TAR_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 200_500
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MANIFEST_NAME = "RELEASE-MANIFEST.json"
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:[.](?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:[+][0-9A-Za-z-]+(?:[.][0-9A-Za-z-]+)*)?$"
)
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}
FORBIDDEN = frozenset('<>:"|?*')


class VerifyError(Exception):
    pass


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise VerifyError(f"unsafe archive path: {path!r}")
    for char in path:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise VerifyError(f"control character in archive path: {path!r}")
    for component in path.split("/"):
        if component in ("", ".", ".."):
            raise VerifyError(f"unsafe archive path component: {path!r}")
        if component.endswith((" ", ".")):
            raise VerifyError(f"platform-ambiguous archive path: {path!r}")
        if any(char in FORBIDDEN for char in component):
            raise VerifyError(f"platform-invalid archive path: {path!r}")
        if component.split(".", 1)[0].upper() in RESERVED:
            raise VerifyError(f"platform-reserved archive path: {path!r}")


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise VerifyError(f"cannot stat {label}: {exc}") from exc
    if size > limit:
        raise VerifyError(f"{label} exceeds size limit")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise VerifyError(f"cannot read {label}: {exc}") from exc


def verify_checksum(archive: Path, checksums: Path) -> tuple[bytes, str]:
    raw = _read_bounded(archive, MAX_COMPRESSED_BYTES, "archive")
    checksum_raw = _read_bounded(checksums, 4096, "checksums")
    expected_name = archive.name.encode("utf-8")
    match = re.fullmatch(rb"([0-9a-f]{64})  ([^\r\n]+)\n", checksum_raw)
    if match is None or match.group(2) != expected_name:
        raise VerifyError("SHA256SUMS is not one canonical line for the archive")
    actual = hashlib.sha256(raw).hexdigest()
    if match.group(1).decode("ascii") != actual:
        raise VerifyError("archive SHA256 mismatch")
    return raw, actual


def decompress_strict(raw: bytes) -> bytes:
    if len(raw) < 10 or raw[:3] != b"\x1f\x8b\x08":
        raise VerifyError("archive is not gzip")
    if raw[3] != 0 or raw[4:8] != b"\x00\x00\x00\x00":
        raise VerifyError("gzip header is not deterministic")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = decoder.decompress(raw, MAX_TAR_BYTES + 1)
        if len(data) > MAX_TAR_BYTES or decoder.unconsumed_tail:
            raise VerifyError("uncompressed tar exceeds size limit")
        remaining = MAX_TAR_BYTES + 1 - len(data)
        data += decoder.flush(remaining)
    except zlib.error as exc:
        raise VerifyError(f"invalid gzip stream: {exc}") from exc
    if len(data) > MAX_TAR_BYTES:
        raise VerifyError("uncompressed tar exceeds size limit")
    if not decoder.eof or decoder.unused_data:
        raise VerifyError("gzip stream is truncated, concatenated, or has trailing data")
    if len(data) % 512 or len(data) < 1024 or data[-1024:] != b"\x00" * 1024:
        raise VerifyError("tar does not have canonical block termination")
    return data


def _json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerifyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _manifest(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise VerifyError("manifest exceeds size limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise VerifyError("manifest root must be an object")
    return value


def inspect_archive(tar_bytes: bytes, archive_name: str) -> tuple[dict[str, object], list[tuple[tarfile.TarInfo, bytes]]]:
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:", errorlevel=2) as tar:
            if tar.pax_headers:
                raise VerifyError("global PAX headers are forbidden")
            members = tar.getmembers()
            if len(members) > MAX_MEMBER_COUNT:
                raise VerifyError("tar member count exceeds limit")
            # tarfile stops at the first end-of-archive marker. Require every
            # byte after the last parsed member to be zero so a second hidden
            # archive/member cannot be smuggled behind that marker.
            if tar.offset > len(tar_bytes) or any(tar_bytes[tar.offset:]):
                raise VerifyError("nonzero data follows the logical tar archive")
            names = [member.name for member in members]
            if [name.encode("utf-8") for name in names] != sorted(name.encode("utf-8") for name in names):
                raise VerifyError("tar members are not in strict bytewise order")
            if len(names) != len(set(names)):
                raise VerifyError("duplicate tar member name")
            keys: dict[str, str] = {}
            payloads: list[tuple[tarfile.TarInfo, bytes]] = []
            total = 0
            roots: set[str] = set()
            for member in members:
                validate_path(member.name)
                key = _path_key(member.name)
                if key in keys:
                    raise VerifyError(f"normalization/case collision: {member.name!r}")
                keys[key] = member.name
                roots.add(member.name.split("/", 1)[0])
                if member.uid != 0 or member.gid != 0 or member.uname != "" or member.gname != "" or member.mtime != 0:
                    raise VerifyError(f"non-normalized metadata: {member.name}")
                if set(member.pax_headers) - {"path"}:
                    raise VerifyError(f"unsafe PAX metadata: {member.name}")
                if "path" in member.pax_headers and member.pax_headers["path"] != member.name:
                    raise VerifyError(f"mismatched PAX path: {member.name}")
                if member.isdir():
                    if member.mode != 0o755 or member.size != 0:
                        raise VerifyError(f"invalid directory metadata: {member.name}")
                    payloads.append((member, b""))
                elif member.isreg():
                    if getattr(member, "sparse", None) is not None:
                        raise VerifyError(f"sparse tar member is forbidden: {member.name}")
                    if member.mode not in (0o644, 0o755) or member.size > MAX_FILE_BYTES:
                        raise VerifyError(f"invalid regular-file metadata: {member.name}")
                    total += member.size
                    if total > MAX_TOTAL_FILE_BYTES:
                        raise VerifyError("total file bytes exceed limit")
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise VerifyError(f"cannot read file payload: {member.name}")
                    data = handle.read(MAX_FILE_BYTES + 1)
                    if len(data) != member.size:
                        raise VerifyError(f"file size mismatch: {member.name}")
                    payloads.append((member, data))
                else:
                    raise VerifyError(f"forbidden tar member type: {member.name}")
    except (tarfile.TarError, OSError) as exc:
        raise VerifyError(f"invalid tar archive: {exc}") from exc

    if len(roots) != 1:
        raise VerifyError("archive must contain exactly one root")
    root = next(iter(roots))
    by_name = {member.name: (member, data) for member, data in payloads}
    root_entry = by_name.get(root)
    if root_entry is None or not root_entry[0].isdir():
        raise VerifyError("archive root directory is missing")
    for member, _ in payloads:
        if member.name == root:
            continue
        parent = member.name.rsplit("/", 1)[0]
        parent_entry = by_name.get(parent)
        if parent_entry is None or not parent_entry[0].isdir():
            raise VerifyError(f"missing directory ancestor: {member.name}")

    manifest_name = f"{root}/{MANIFEST_NAME}"
    manifests = [(member, data) for member, data in payloads if member.name == manifest_name and member.isreg()]
    if len(manifests) != 1:
        raise VerifyError("archive must contain exactly one root manifest")
    manifest = _manifest(manifests[0][1])
    expected_keys = {"schemaVersion", "version", "commit", "archiveRoot", "timestampPolicy", "entries"}
    if set(manifest) != expected_keys or manifest.get("schemaVersion") != 1 or manifest.get("timestampPolicy") != "unix-epoch":
        raise VerifyError("manifest schema or keys are invalid")
    version = manifest.get("version")
    commit = manifest.get("commit")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise VerifyError("manifest version is invalid")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise VerifyError("manifest commit is invalid")
    expected_root = f"tracehelix-{version}"
    if root != expected_root or manifest.get("archiveRoot") != root:
        raise VerifyError("manifest archive root mismatch")
    if archive_name != f"{root}-source.tar.gz":
        raise VerifyError("archive filename does not match manifest version")

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise VerifyError("manifest entries must be an array")
    paths: list[str] = []
    declared: dict[str, dict[str, object]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path", "mode", "size", "sha256"}:
            raise VerifyError("manifest entry schema is invalid")
        path = raw_entry.get("path")
        mode = raw_entry.get("mode")
        size = raw_entry.get("size")
        digest = raw_entry.get("sha256")
        if not isinstance(path, str):
            raise VerifyError("manifest path must be a string")
        validate_path(path)
        if mode not in ("0644", "0755") or type(size) is not int or size < 0 or size > MAX_FILE_BYTES:
            raise VerifyError(f"manifest metadata is invalid: {path}")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise VerifyError(f"manifest SHA256 is invalid: {path}")
        if path == MANIFEST_NAME or path in declared:
            raise VerifyError(f"duplicate/self manifest entry: {path}")
        paths.append(path)
        declared[path] = raw_entry
    if [path.encode("utf-8") for path in paths] != sorted(path.encode("utf-8") for path in paths):
        raise VerifyError("manifest entries are not bytewise sorted")
    if len({_path_key(path) for path in paths}) != len(paths):
        raise VerifyError("manifest path normalization/case collision")

    actual_files: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    prefix = root + "/"
    for member, data in payloads:
        if member.isreg() and member.name != manifest_name:
            actual_files[member.name[len(prefix):]] = (member, data)
    if set(declared) != set(actual_files):
        raise VerifyError("manifest file inventory does not match archive")
    for path, entry in declared.items():
        member, data = actual_files[path]
        if entry["size"] != len(data) or entry["mode"] != f"{member.mode:04o}" or entry["sha256"] != hashlib.sha256(data).hexdigest():
            raise VerifyError(f"manifest payload mismatch: {path}")
    return manifest, payloads


def extract_safely(extract_dir: Path, payloads: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    if not extract_dir.is_absolute() or extract_dir.exists():
        raise VerifyError("--extract-dir must be an absolute non-existing path")
    parent = extract_dir.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent:
        raise VerifyError("--extract-dir parent must be a real existing directory")
    try:
        extract_dir.mkdir(mode=0o755)
        for member, data in payloads:
            destination = extract_dir / member.name
            if member.isdir():
                destination.mkdir(mode=0o755)
                os.chmod(destination, 0o755)
            else:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, member.mode)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(destination, member.mode)
    except BaseException:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise


def verify_and_extract(archive: Path, checksums: Path, extract_dir: Path) -> dict[str, object]:
    if not archive.is_absolute() or not checksums.is_absolute():
        raise VerifyError("--archive and --checksums must be absolute paths")
    raw, digest = verify_checksum(archive, checksums)
    tar_bytes = decompress_strict(raw)
    manifest, payloads = inspect_archive(tar_bytes, archive.name)
    extract_safely(extract_dir, payloads)
    entries = manifest.get("entries")
    if not isinstance(entries, list):  # inspect_archive already enforces this
        raise VerifyError("manifest entries are invalid")
    return {
        "archive": str(archive),
        "sha256": digest,
        "version": manifest["version"],
        "commit": manifest["commit"],
        "extractDir": str(extract_dir),
        "sourceFileCount": len(entries),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--extract-dir", required=True)
    args = parser.parse_args(argv)
    try:
        summary = verify_and_extract(Path(args.archive), Path(args.checksums), Path(args.extract_dir))
    except (VerifyError, OSError) as exc:
        print(f"verify_release_bundle: {exc}", file=sys.stderr)
        return 1
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
