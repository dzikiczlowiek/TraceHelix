#!/usr/bin/env python3
"""Real-process and focused adversarial tests for release asset handoff code."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release_bundle as B  # noqa: E402
import release_assets as R  # noqa: E402


class ReleaseAssetsFixture(unittest.TestCase):
    version = "1.2.3"

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="tracehelix-assets-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.assets = self.temp / "assets"
        self.assets.mkdir()
        root = f"tracehelix-{self.version}"
        files = [("README.md", 0o644, b"fixture\n"), ("VERSION", 0o644, b"1.2.3\n")]
        manifest = B.build_manifest(self.version, "a" * 40, root, files)
        archive = B.gzip_deterministic(B.build_tar(root, files + [(B.MANIFEST_NAME, 0o644, manifest)]))
        names = R.asset_names(self.version)
        (self.assets / names[0]).write_bytes(archive)
        (self.assets / "SHA256SUMS").write_text(
            f"{hashlib.sha256(archive).hexdigest()}  {names[0]}\n", encoding="ascii"
        )
        (self.assets / "RELEASE-MANIFEST.json").write_bytes(manifest)
        (self.assets / "RELEASE-NOTES.md").write_text("# Notes\n", encoding="utf-8")
        (self.assets / names[4]).write_text('{"bomFormat":"CycloneDX"}\n', encoding="utf-8")
        (self.assets / "tracehelix-api.spdx.json").write_text("{}\n", encoding="utf-8")
        (self.assets / "tracehelix-web.spdx.json").write_text("{}\n", encoding="utf-8")

    def cli(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(Path(R.__file__).resolve()), *args],
            text=True,
            capture_output=True,
            check=False,
            **kwargs,
        )


class TestCanonicalHandoff(ReleaseAssetsFixture):
    def test_assemble_digest_is_deterministic_and_written_as_job_output(self) -> None:
        output = self.temp / "github-output"
        first = self.cli("assemble", "--directory", str(self.assets), "--version", self.version, "--github-output", str(output))
        second = R.assemble(self.assets, self.version)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(second, first.stdout.strip())
        self.assertEqual(f"handoff_digest={second}\n", output.read_text(encoding="utf-8"))

    def test_each_asset_mutation_including_checksum_is_rejected_by_publish(self) -> None:
        expected = R.assemble(self.assets, self.version)
        for name in R.asset_names(self.version):
            with self.subTest(name=name):
                original = (self.assets / name).read_bytes()
                (self.assets / name).write_bytes(original + b"tamper")
                result = self.cli("verify-publish", "--directory", str(self.assets), "--version", self.version, "--tag", "v1.2.3", "--expected-digest", expected)
                self.assertNotEqual(0, result.returncode, result.stderr)
                (self.assets / name).write_bytes(original)

    def test_rejects_extra_nested_symlink_and_fifo_handoff_content(self) -> None:
        mutations = {
            "extra": lambda: (self.assets / "EXTRA").write_bytes(b"x"),
            "nested": lambda: (self.assets / "nested").mkdir(),
            "symlink": lambda: (self.assets / "link").symlink_to("SHA256SUMS"),
        }
        if hasattr(os, "mkfifo"):
            mutations["fifo"] = lambda: os.mkfifo(self.assets / "pipe")
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                mutate()
                with self.assertRaises(R.ReleaseAssetError):
                    R.assemble(self.assets, self.version)
                for path in self.assets.iterdir():
                    if path.name not in R.asset_names(self.version):
                        if path.is_dir() and not path.is_symlink():
                            shutil.rmtree(path)
                        else:
                            path.unlink()

    def test_publish_requires_tag_and_assemble_digest(self) -> None:
        digest = R.assemble(self.assets, self.version)
        with self.assertRaises(R.ReleaseAssetError):
            R.verify_publish(self.assets, self.version, "v9.9.9", digest)
        with self.assertRaises(R.ReleaseAssetError):
            R.verify_publish(self.assets, self.version, "v1.2.3", "0" * 64)


class TestVerifiedExport(ReleaseAssetsFixture):
    def test_real_process_rejects_nonempty_collision_and_symlink_destination(self) -> None:
        for label, prepare in (
            ("nonempty", lambda p: (p / "old").write_bytes(b"old")),
            ("collision", lambda p: (p / R.asset_names(self.version)[0]).write_bytes(b"racer")),
        ):
            with self.subTest(label=label):
                destination = self.temp / label
                destination.mkdir()
                prepare(destination)
                result = self.cli("export-verified", "--archive", str(self.assets / R.asset_names(self.version)[0]), "--checksums", str(self.assets / "SHA256SUMS"), "--manifest", str(self.assets / "RELEASE-MANIFEST.json"), "--version", self.version, "--destination", str(destination))
                self.assertNotEqual(0, result.returncode, result.stderr)
                if label == "collision":
                    self.assertEqual(b"racer", (destination / R.asset_names(self.version)[0]).read_bytes())
        real = self.temp / "real"
        real.mkdir()
        link = self.temp / "symlink"
        link.symlink_to(real, target_is_directory=True)
        result = self.cli("export-verified", "--archive", str(self.assets / R.asset_names(self.version)[0]), "--checksums", str(self.assets / "SHA256SUMS"), "--manifest", str(self.assets / "RELEASE-MANIFEST.json"), "--version", self.version, "--destination", str(link))
        self.assertNotEqual(0, result.returncode, result.stderr)

    def test_exclusive_create_preserves_a_racing_target(self) -> None:
        destination = self.temp / "race"
        destination.mkdir()
        archive_name = R.asset_names(self.version)[0]
        real_open = R.os.open
        raced = False

        def race_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            nonlocal raced
            if path == archive_name and flags & os.O_CREAT and not raced:
                raced = True
                racer = real_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
                os.write(racer, b"racer")
                os.close(racer)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        R.os.open = race_open  # type: ignore[assignment]
        try:
            with self.assertRaises(FileExistsError):
                R.export_verified(self.assets / archive_name, self.assets / "SHA256SUMS", self.assets / "RELEASE-MANIFEST.json", self.version, destination)
        finally:
            R.os.open = real_open  # type: ignore[assignment]
        self.assertTrue(raced)
        self.assertEqual(b"racer", (destination / archive_name).read_bytes())

    def test_partial_failure_rolls_back_only_owned_files(self) -> None:
        destination = self.temp / "partial"
        destination.mkdir()
        real_write = R.os.write
        writes = 0

        def fail_second(fd: int, data: object) -> int:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("injected export failure")
            return real_write(fd, data)  # type: ignore[arg-type]

        R.os.write = fail_second  # type: ignore[assignment]
        try:
            with self.assertRaises(OSError):
                R.export_verified(self.assets / R.asset_names(self.version)[0], self.assets / "SHA256SUMS", self.assets / "RELEASE-MANIFEST.json", self.version, destination)
        finally:
            R.os.write = real_write  # type: ignore[assignment]
        self.assertEqual([], list(destination.iterdir()))

    def test_rollback_preserves_foreign_inode_replacement(self) -> None:
        destination = self.temp / "foreign-replacement"
        destination.mkdir()
        archive_name = R.asset_names(self.version)[0]
        real_write = R.os.write
        replaced = False

        def replace_then_fail(fd: int, data: object) -> int:
            nonlocal replaced
            if not replaced:
                replaced = True
                (destination / archive_name).unlink()
                (destination / archive_name).write_bytes(b"foreign")
                raise OSError("injected failure after inode replacement")
            return real_write(fd, data)  # type: ignore[arg-type]

        R.os.write = replace_then_fail  # type: ignore[assignment]
        try:
            with self.assertRaises(OSError):
                R.export_verified(
                    self.assets / archive_name,
                    self.assets / "SHA256SUMS",
                    self.assets / "RELEASE-MANIFEST.json",
                    self.version,
                    destination,
                )
        finally:
            R.os.write = real_write  # type: ignore[assignment]
        self.assertTrue(replaced)
        self.assertEqual(b"foreign", (destination / archive_name).read_bytes())

    def test_canonical_forbidden_root_rejects_dotdot_alias(self) -> None:
        forbidden = self.temp / "checkout"
        destination = forbidden / "empty"
        destination.mkdir(parents=True)
        (self.temp / "other").mkdir()
        alias = self.temp / "other" / ".." / "checkout" / "empty"
        with self.assertRaises(R.ReleaseAssetError):
            R.export_verified(
                self.assets / R.asset_names(self.version)[0],
                self.assets / "SHA256SUMS",
                self.assets / "RELEASE-MANIFEST.json",
                self.version,
                alias,
                (forbidden,),
            )

    def test_term_during_export_rolls_back_partial_files(self) -> None:
        destination = self.temp / "term"
        destination.mkdir()
        real_write = R.os.write
        sent = False

        def terminate(fd: int, data: object) -> int:
            nonlocal sent
            if not sent:
                sent = True
                os.kill(os.getpid(), signal.SIGTERM)
            return real_write(fd, data)  # type: ignore[arg-type]

        R.os.write = terminate  # type: ignore[assignment]
        try:
            with self.assertRaises(R.ExportInterrupted):
                R.export_verified(self.assets / R.asset_names(self.version)[0], self.assets / "SHA256SUMS", self.assets / "RELEASE-MANIFEST.json", self.version, destination)
        finally:
            R.os.write = real_write  # type: ignore[assignment]
        self.assertEqual([], list(destination.iterdir()))


class TestReleaseCreation(ReleaseAssetsFixture):
    def _fake_gh(self, mode: str) -> Path:
        gh = self.temp / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "open(os.environ['ARGS'], 'w').write('\\n'.join(sys.argv[1:]))\n"
            "if sys.argv[1:3] == ['api', '--include']:\n"
            "    status = int(os.environ['VIEW'])\n"
            "    print(f'HTTP/2.0 {status} ' + ('Not Found' if status == 404 else 'Server Error'))\n"
            "    sys.exit(0 if status == 200 else 1)\n"
            "sys.exit(int(os.environ['CREATE']))\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        return gh

    def test_existing_release_fails_missing_may_proceed_and_create_race_fails(self) -> None:
        gh = self._fake_gh("unused")
        arguments = self.temp / "args"
        old = os.environ.copy()
        os.environ.update(
            {
                "ARGS": str(arguments),
                "VIEW": "200",
                "CREATE": "0",
                "GITHUB_REPOSITORY": "owner/repo",
            }
        )
        try:
            with self.assertRaises(R.ReleaseAssetError):
                R.assert_release_absent("v1.2.3", str(gh))
            os.environ["VIEW"] = "500"
            with self.assertRaises(R.ReleaseAssetError):
                R.assert_release_absent("v1.2.3", str(gh))
            os.environ["VIEW"] = "404"
            R.assert_release_absent("v1.2.3", str(gh))
            digest = R.assemble(self.assets, self.version)
            R.create_release(self.assets, self.version, "v1.2.3", digest, str(gh))
            argv = arguments.read_text(encoding="utf-8").splitlines()
            self.assertEqual(["release", "create", "v1.2.3", "--verify-tag"], argv[:4])
            self.assertEqual([str(self.assets / name) for name in R.asset_names(self.version)], argv[-7:])
            os.environ["CREATE"] = "1"  # create race/existing tag/release
            with self.assertRaises(subprocess.CalledProcessError):
                R.create_release(self.assets, self.version, "v1.2.3", digest, str(gh))
            archive = self.assets / R.asset_names(self.version)[0]
            archive.write_bytes(archive.read_bytes() + b"tamper-before-create")
            with self.assertRaises(R.ReleaseAssetError):
                R.create_release(self.assets, self.version, "v1.2.3", digest, str(gh))
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
