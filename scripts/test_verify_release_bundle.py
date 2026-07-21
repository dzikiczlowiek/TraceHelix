#!/usr/bin/env python3
"""Adversarial tests for verify_release_bundle.py."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release_bundle as B  # noqa: E402
import verify_release_bundle as V  # noqa: E402


class BundleFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="thx-verify-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.version = "0.1.0"
        self.root = "tracehelix-0.1.0"
        self.archive = self.tmp / f"{self.root}-source.tar.gz"
        self.checksums = self.tmp / "SHA256SUMS"
        self.files = [
            ("README.md", 0o644, b"# TraceHelix\n"),
            ("VERSION", 0o644, b"0.1.0\n"),
            ("scripts/run.sh", 0o755, b"#!/bin/sh\nexit 0\n"),
        ]
        manifest = B.build_manifest(self.version, "a" * 40, self.root, self.files)
        self.base_tar = B.build_tar(self.root, self.files + [(B.MANIFEST_NAME, 0o644, manifest)])
        self._write_gzip(B.gzip_deterministic(self.base_tar))

    def _write_gzip(self, compressed: bytes) -> None:
        self.archive.write_bytes(compressed)
        digest = hashlib.sha256(compressed).hexdigest()
        self.checksums.write_text(f"{digest}  {self.archive.name}\n", encoding="ascii")

    def _extract(self, name: str = "extract") -> Path:
        return self.tmp / name

    def _specs(self) -> list[dict[str, object]]:
        specs: list[dict[str, object]] = []
        with tarfile.open(fileobj=io.BytesIO(self.base_tar), mode="r:") as tar:
            for member in tar.getmembers():
                data = tar.extractfile(member).read() if member.isfile() else b""
                specs.append(
                    {
                        "name": member.name,
                        "type": member.type,
                        "mode": member.mode,
                        "data": data,
                        "mtime": member.mtime,
                        "linkname": member.linkname,
                    }
                )
        return specs

    def _tar(self, specs: list[dict[str, object]]) -> bytes:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for spec in specs:
                data = spec.get("data", b"")
                assert isinstance(data, bytes)
                info = tarfile.TarInfo(str(spec["name"]))
                info.type = spec.get("type", tarfile.REGTYPE)  # type: ignore[assignment]
                info.mode = int(spec.get("mode", 0o644))
                info.uid = int(spec.get("uid", 0))
                info.gid = int(spec.get("gid", 0))
                info.uname = str(spec.get("uname", ""))
                info.gname = str(spec.get("gname", ""))
                info.mtime = int(spec.get("mtime", 0))
                info.linkname = str(spec.get("linkname", ""))
                if info.type == tarfile.REGTYPE:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    info.size = 0
                    tar.addfile(info)
        return out.getvalue()

    def _mutate_manifest(self, mutate) -> bytes:
        specs = self._specs()
        for spec in specs:
            if spec["name"] == f"{self.root}/{B.MANIFEST_NAME}":
                manifest = json.loads(spec["data"])
                mutate(manifest)
                spec["data"] = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        return self._tar(specs)


class TestSuccess(BundleFixture):
    def test_verify_and_extract(self) -> None:
        target = self._extract()
        summary = V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), target.resolve())
        self.assertEqual(summary["version"], "0.1.0")
        self.assertEqual(summary["commit"], "a" * 40)
        extracted = target / self.root
        self.assertEqual((extracted / "README.md").read_bytes(), b"# TraceHelix\n")
        self.assertEqual((extracted / "scripts/run.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((extracted / "README.md").stat().st_mode & 0o777, 0o644)

    def test_cli_success(self) -> None:
        target = self._extract()
        rc = V.main(["--archive", str(self.archive.resolve()), "--checksums", str(self.checksums.resolve()), "--extract-dir", str(target.resolve())])
        self.assertEqual(rc, 0)
        self.assertTrue((target / self.root / V.MANIFEST_NAME).is_file())


class TestChecksumAndGzip(BundleFixture):
    def test_checksum_tamper(self) -> None:
        self.archive.write_bytes(self.archive.read_bytes() + b"x")
        with self.assertRaises(V.VerifyError):
            V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), self._extract().resolve())

    def test_noncanonical_checksum_line(self) -> None:
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest().upper()
        self.checksums.write_text(f"{digest} *{self.archive.name}\n", encoding="ascii")
        with self.assertRaises(V.VerifyError):
            V.verify_checksum(self.archive, self.checksums)

    def test_gzip_trailing_data_rejected(self) -> None:
        self._write_gzip(self.archive.read_bytes() + b"TRAIL")
        with self.assertRaises(V.VerifyError):
            V.decompress_strict(self.archive.read_bytes())

    def test_concatenated_gzip_rejected(self) -> None:
        self._write_gzip(self.archive.read_bytes() + gzip.compress(b"second", mtime=0))
        with self.assertRaises(V.VerifyError):
            V.decompress_strict(self.archive.read_bytes())

    def test_compressed_bound(self) -> None:
        old = V.MAX_COMPRESSED_BYTES
        V.MAX_COMPRESSED_BYTES = 8
        try:
            with self.assertRaises(V.VerifyError):
                V.verify_checksum(self.archive, self.checksums)
        finally:
            V.MAX_COMPRESSED_BYTES = old


class TestTarAttacks(BundleFixture):
    def _reject(self, specs: list[dict[str, object]]) -> None:
        self._write_gzip(B.gzip_deterministic(self._tar(specs)))
        target = self._extract()
        with self.assertRaises(V.VerifyError):
            V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), target.resolve())
        self.assertFalse(target.exists())

    def test_traversal_rejected(self) -> None:
        specs = self._specs()
        specs[-1]["name"] = "../escape"
        self._reject(specs)
        self.assertFalse((self.tmp.parent / "escape").exists())

    def test_absolute_rejected(self) -> None:
        specs = self._specs()
        specs[-1]["name"] = "/tmp/escape"
        self._reject(specs)

    def test_symlink_and_hardlink_rejected(self) -> None:
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(kind=kind):
                specs = self._specs()
                specs[-1]["type"] = kind
                specs[-1]["linkname"] = "/etc/passwd"
                self._reject(specs)

    def test_duplicate_rejected(self) -> None:
        specs = self._specs()
        specs.append(dict(specs[-1]))
        specs.sort(key=lambda item: str(item["name"]).encode())
        self._reject(specs)

    def test_unsorted_rejected(self) -> None:
        self._reject(list(reversed(self._specs())))

    def test_nonzero_data_after_logical_tar_end_rejected(self) -> None:
        tampered = bytearray(self.base_tar)
        with tarfile.open(fileobj=io.BytesIO(self.base_tar), mode="r:") as tar:
            tar.getmembers()
            hidden_offset = tar.offset + 1024
        self.assertLess(hidden_offset, len(tampered))
        tampered[hidden_offset] = 1
        self._write_gzip(B.gzip_deterministic(bytes(tampered)))
        target = self._extract()
        with self.assertRaises(V.VerifyError):
            V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), target.resolve())
        self.assertFalse(target.exists())

    def test_non_normalized_metadata_rejected(self) -> None:
        for field, value in (("mtime", 1), ("uid", 1), ("mode", 0o600)):
            with self.subTest(field=field):
                specs = self._specs()
                specs[1][field] = value
                self._reject(specs)

    def test_file_size_bound(self) -> None:
        old = V.MAX_FILE_BYTES
        V.MAX_FILE_BYTES = 4
        try:
            self._reject(self._specs())
        finally:
            V.MAX_FILE_BYTES = old


class TestManifestAttacks(BundleFixture):
    def _reject_tar(self, tar_bytes: bytes) -> None:
        self._write_gzip(B.gzip_deterministic(tar_bytes))
        target = self._extract()
        with self.assertRaises(V.VerifyError):
            V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), target.resolve())
        self.assertFalse(target.exists())

    def test_hash_mismatch(self) -> None:
        self._reject_tar(self._mutate_manifest(lambda m: m["entries"][0].__setitem__("sha256", "0" * 64)))

    def test_missing_manifest_entry(self) -> None:
        self._reject_tar(self._mutate_manifest(lambda m: m["entries"].pop()))

    def test_extra_manifest_entry(self) -> None:
        def add(m):
            m["entries"].append({"path": "zzz", "mode": "0644", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()})
        self._reject_tar(self._mutate_manifest(add))

    def test_wrong_commit_and_root(self) -> None:
        for field, value in (("commit", "bad"), ("archiveRoot", "other")):
            with self.subTest(field=field):
                self._reject_tar(self._mutate_manifest(lambda m, f=field, v=value: m.__setitem__(f, v)))

    def test_manifest_duplicate_json_key(self) -> None:
        specs = self._specs()
        for spec in specs:
            if spec["name"] == f"{self.root}/{B.MANIFEST_NAME}":
                spec["data"] = b'{"schemaVersion":1,"schemaVersion":1}\n'
        self._reject_tar(self._tar(specs))


class TestExtractionRollback(BundleFixture):
    def test_partial_write_failure_removes_extract_dir(self) -> None:
        target = self._extract().resolve()
        real_open = V.os.open
        calls = 0

        def fail_second(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal calls
            if flags & os.O_CREAT:
                calls += 1
                if calls == 2:
                    raise OSError("injected write failure")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        V.os.open = fail_second
        try:
            with self.assertRaises(OSError):
                V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), target)
        finally:
            V.os.open = real_open
        self.assertFalse(target.exists())

    def test_existing_extract_dir_rejected(self) -> None:
        target = self._extract()
        target.mkdir()
        with self.assertRaises(V.VerifyError):
            V.verify_and_extract(self.archive.resolve(), self.checksums.resolve(), target.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
