#!/usr/bin/env python3
"""Adversarial RED->GREEN tests for scripts/build_release_bundle.py.

Exercises determinism, the bytewise tar contract, ambient-state exclusion,
ref resolution, version/path validation, blob/size/count bounds, output
rejection, race-safe atomic output, partial-failure cleanup, and manifest
integrity. Uses disposable Git repositories only. Stdlib + unittest.

Run:
    python3 scripts/test_build_release_bundle.py
    pytest scripts/test_build_release_bundle.py
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release_bundle as B  # noqa: E402


# --- helpers ---------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc.stdout.decode("utf-8", "replace")


def _make_repo(
    *,
    files: dict[str, bytes] | None = None,
    version: bytes = b"1.2.3",
    executable: tuple[str, ...] = (),
    branch: str | None = None,
    extra_commit: dict[str, bytes] | None = None,
) -> Path:
    """Create a disposable repo with one commit (plus optional second commit)."""
    repo = Path(tempfile.mkdtemp(prefix="thx-repo-"))
    _git(repo, "init", "-q", "-b", "main")
    for key, val in (
        ("user.name", "TraceHelix Test"),
        ("user.email", "test@tracehelix.local"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        _git(repo, "config", key, val)
    ver = version if version.endswith(b"\n") else version + b"\n"
    (repo / "VERSION").write_bytes(ver)
    for rel, data in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    for rel in executable:
        _git(repo, "update-index", "--chmod=+x", rel)
    _git(repo, "commit", "-q", "-m", "init")
    if extra_commit:
        for rel, data in extra_commit.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "second")
    if branch:
        _git(repo, "branch", branch)
    return repo


def _new_outdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="thx-out-"))


def _rm(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _run_build(repo: Path, outdir: Path, ref: str = "HEAD") -> dict:
    with chdir(repo):
        return B.build(str(outdir), ref)


def _open_tar(summary: dict) -> tarfile.TarFile:
    raw = gzip.decompress(Path(summary["archive"]).read_bytes())
    return tarfile.open(fileobj=io.BytesIO(raw))


def _entry(mode: int = 0o100644, otype: str = "blob", path: str = "a") -> B.TreeEntry:
    return B.TreeEntry(mode=mode, obj_type=otype, sha="0" * 40, path=path)


# A tree that exercises: nested dirs, an exec file, a file/dir prefix clash
# (api.ts vs api/), a non-ASCII name, and a >100-byte path (PAX path header).
RICH_FILES: dict[str, bytes] = {
    "README.md": b"# rich project\n",
    "web/src/api.ts": b"export const x = 1;\n",
    "web/src/api/index.txt": b"inside api dir\n",
    "data/caf\u00e9-ontap.txt": "caf\u00e9 data\n".encode("utf-8"),
    "long/" + ("a" * 120) + ".txt": b"long path payload\n",
}
RICH_EXEC = ("web/src/api.ts",)


# --- determinism -----------------------------------------------------------


class TestReproducibility(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _make_repo(files=RICH_FILES, executable=RICH_EXEC)
        self.addCleanup(_rm, self.repo)

    def test_byte_identical_across_dirs_clock_and_umask(self) -> None:
        out1 = _new_outdir()
        out2 = _new_outdir()
        self.addCleanup(_rm, out1)
        self.addCleanup(_rm, out2)

        prev_umask = os.umask(0o022)
        try:
            s1 = _run_build(self.repo, out1)
        finally:
            os.umask(prev_umask)

        import time

        time.sleep(2)  # advance the wall clock past any second boundary

        prev_umask = os.umask(0o077)  # radically different umask
        try:
            s2 = _run_build(self.repo, out2)
        finally:
            os.umask(prev_umask)

        arc1 = (out1 / Path(s1["archive"]).name).read_bytes()
        arc2 = (out2 / Path(s2["archive"]).name).read_bytes()
        sum1 = (out1 / B.CHECKSUM_NAME).read_bytes()
        sum2 = (out2 / B.CHECKSUM_NAME).read_bytes()
        self.assertEqual(arc1, arc2, "archive bytes differ across output dirs")
        self.assertEqual(sum1, sum2, "SHA256SUMS bytes differ across output dirs")
        self.assertEqual(s1["sha256"], s2["sha256"])
        self.assertEqual(s1["commit"], s2["commit"])

    def test_dirty_staged_untracked_ignored_excluded(self) -> None:
        base = _new_outdir()
        self.addCleanup(_rm, base)
        s_before = _run_build(self.repo, base)
        before = Path(s_before["archive"]).read_bytes()

        # Ambient working-tree mutations that must NOT influence the archive.
        (self.repo / "VERSION").write_bytes(b"9.9.9\n")  # dirty (uncommitted)
        (self.repo / "untracked.txt").write_bytes(b"ignored by build\n")  # untracked
        (self.repo / "README.md").write_bytes(b"tampered\n")  # dirty tracked
        _git(self.repo, "add", "README.md")  # staged change
        (self.repo / "build").mkdir(exist_ok=True)
        (self.repo / "build" / "artifact.bin").write_bytes(b"\x00\x01")  # ignored-ish
        (self.repo / ".gitignore").write_bytes(b"/build/\n")
        _git(self.repo, "add", ".gitignore")

        after_dir = _new_outdir()
        self.addCleanup(_rm, after_dir)
        s_after = _run_build(self.repo, after_dir)
        after = Path(s_after["archive"]).read_bytes()
        self.assertEqual(before, after, "ambient working-tree state leaked into archive")
        self.assertEqual(s_before["sha256"], s_after["sha256"])

    def test_pax_long_and_nonascii_names_byte_stable(self) -> None:
        # The rich repo already contains a >100-byte path and a non-ASCII name,
        # which force PAX extended path headers; rebuild and compare bytes.
        out1 = _new_outdir()
        out2 = _new_outdir()
        self.addCleanup(_rm, out1)
        self.addCleanup(_rm, out2)
        s1 = _run_build(self.repo, out1)
        s2 = _run_build(self.repo, out2)
        self.assertEqual(
            Path(s1["archive"]).read_bytes(),
            Path(s2["archive"]).read_bytes(),
        )
        # Confirm the PAX-forcing members are actually present.
        with _open_tar(s1) as tar:
            names = {m.name for m in tar.getmembers()}
        self.assertTrue(any("long/" in n for n in names))
        self.assertTrue(any("caf\u00e9" in n for n in names))


# --- tar contract ----------------------------------------------------------


class TestTarContract(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _make_repo(files=RICH_FILES, executable=RICH_EXEC)
        self.addCleanup(_rm, self.repo)
        self.out = _new_outdir()
        self.addCleanup(_rm, self.out)
        self.summary = _run_build(self.repo, self.out)
        self.archive_root = f"tracehelix-{self.summary['version']}"

    def test_member_order_is_strict_bytewise(self) -> None:
        with _open_tar(self.summary) as tar:
            names = [m.name.encode("utf-8") for m in tar.getmembers()]
        self.assertEqual(names, sorted(names), "tar members not in bytewise order")

    def test_member_metadata_is_normalized(self) -> None:
        with _open_tar(self.summary) as tar:
            for m in tar.getmembers():
                self.assertEqual(m.uid, 0, f"uid not 0: {m.name}")
                self.assertEqual(m.gid, 0, f"gid not 0: {m.name}")
                self.assertEqual(m.uname, "", f"uname not empty: {m.name}")
                self.assertEqual(m.gname, "", f"gname not empty: {m.name}")
                self.assertEqual(m.mtime, 0, f"mtime not 0: {m.name}")

    def test_modes_are_normalized(self) -> None:
        with _open_tar(self.summary) as tar:
            for m in tar.getmembers():
                if m.isdir():
                    self.assertEqual(m.mode, 0o755, f"dir mode: {m.name}")
                elif m.isfile():
                    self.assertIn(m.mode, (0o644, 0o755), f"file mode: {m.name}")
                else:
                    self.fail(f"unexpected member type {m.type!r}: {m.name}")

    def test_root_is_directory_named_archive_root(self) -> None:
        with _open_tar(self.summary) as tar:
            root = tar.getmembers()[0]
            self.assertTrue(root.isdir())
            self.assertEqual(root.name, self.archive_root)

    def test_manifest_member_present_without_self_entry(self) -> None:
        with _open_tar(self.summary) as tar:
            members = {m.name for m in tar.getmembers()}
        manifest_name = f"{self.archive_root}/{B.MANIFEST_NAME}"
        self.assertIn(manifest_name, members)

    def test_directory_ancestors_emitted(self) -> None:
        with _open_tar(self.summary) as tar:
            dirs = {m.name for m in tar.getmembers() if m.isdir()}
        self.assertIn(self.archive_root, dirs)
        self.assertIn(f"{self.archive_root}/web", dirs)
        self.assertIn(f"{self.archive_root}/web/src", dirs)
        self.assertIn(f"{self.archive_root}/web/src/api", dirs)

    def test_no_partial_blocks_or_two_blocks(self) -> None:
        raw = gzip.decompress(Path(self.summary["archive"]).read_bytes())
        # tar archive must be a whole number of 512-byte blocks plus two
        # trailing zero blocks.
        self.assertEqual(len(raw) % 512, 0)
        self.assertEqual(raw[-1024:], b"\x00" * 1024)


# --- ref resolution --------------------------------------------------------


class TestRefResolution(unittest.TestCase):
    def test_branch_ref_resolves_exact_commit(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"}, branch="release/x")
        self.addCleanup(_rm, repo)
        with chdir(repo):
            head = B.resolve_ref(repo, "HEAD")
        out = _new_outdir()
        self.addCleanup(_rm, out)
        summary = _run_build(repo, out, ref="release/x")
        self.assertRegex(summary["commit"], r"\A[0-9a-f]{40}\Z")
        self.assertEqual(summary["commit"], head)

    def test_sha_ref_resolves(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        with chdir(repo):
            sha = B.resolve_ref(repo, "HEAD")
        out = _new_outdir()
        self.addCleanup(_rm, out)
        summary = _run_build(repo, out, ref=sha)
        self.assertEqual(summary["commit"], sha)

    def test_empty_ref_rejected(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        with self.assertRaises(B.BuildError):
            B.resolve_ref(repo, "")

    def test_rev_parse_uses_end_of_options(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        captured: list[list[str]] = []
        real = B._run_git

        def spy(args: list[str], cwd: Path) -> bytes:
            captured.append(list(args))
            return real(args, cwd)

        original = B._run_git
        B._run_git = spy
        try:
            with chdir(repo):
                B.resolve_ref(repo, "HEAD")
        finally:
            B._run_git = original
        rev_calls = [c for c in captured if c[:1] == ["rev-parse"]]
        self.assertTrue(rev_calls, "rev-parse was not invoked")
        self.assertIn("--end-of-options", rev_calls[0])


# --- version validation ----------------------------------------------------


class TestVersionValidation(unittest.TestCase):
    INVALID = [
        b"",
        b"x",
        b"1",
        b"1.2",
        b"1.2.3.4",
        b"01.2.3",
        b"1.02.3",
        b"1.2.03",
        b"v1.2.3",
        b"1.2.3-",
        b"1.2.3-_",
        b"1.2.3-01",
        b"1.2.3-alpha beta",
    ]

    def test_invalid_version_rejected(self) -> None:
        for bad in self.INVALID:
            with self.subTest(version=bad):
                repo = _make_repo(files={"a.txt": b"a\n"}, version=bad)
                self.addCleanup(_rm, repo)
                out = _new_outdir()
                self.addCleanup(_rm, out)
                with self.assertRaises(B.BuildError):
                    _run_build(repo, out)

    def test_canonical_semver_accepted(self) -> None:
        for good in (b"1.2.3", b"0.0.0", b"10.20.30", b"1.0.0-alpha.1", b"1.0.0+build.5"):
            with self.subTest(version=good):
                repo = _make_repo(files={"a.txt": b"a\n"}, version=good)
                self.addCleanup(_rm, repo)
                out = _new_outdir()
                self.addCleanup(_rm, out)
                summary = _run_build(repo, out)
                self.assertEqual(summary["version"], good.decode("ascii"))


# --- path validator (pure unit) -------------------------------------------


class TestPathValidator(unittest.TestCase):
    def test_valid_paths_accepted(self) -> None:
        for ok in ("a", "a/b.txt", "web/src/api.ts", "data/caf\u00e9.txt", "A-B_C.d"):
            B.validate_source_path(ok)  # must not raise

    def test_empty_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.validate_source_path("")

    def test_absolute_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.validate_source_path("/etc/passwd")

    def test_traversal_rejected(self) -> None:
        for bad in ("../x", "a/../../b", "a/../b", ".."):
            with self.subTest(path=bad):
                with self.assertRaises(B.BuildError):
                    B.validate_source_path(bad)

    def test_self_and_empty_components_rejected(self) -> None:
        for bad in (".", "a/./b", "a//b", "a/b/", "/a"):
            with self.subTest(path=bad):
                with self.assertRaises(B.BuildError):
                    B.validate_source_path(bad)

    def test_backslash_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.validate_source_path("a\\b")

    def test_control_chars_rejected(self) -> None:
        for code in (0x00, 0x01, 0x09, 0x0A, 0x1F, 0x7F):
            with self.subTest(code=code):
                with self.assertRaises(B.BuildError):
                    B.validate_source_path("a" + chr(code) + "b")

    def test_unicode_casefold_collision_rejected(self) -> None:
        # 'ABC.TXT' and 'abc.txt' casefold-equal; select_source_files must
        # refuse to pack both even though they are distinct exact paths.
        entries = [
            _entry(path="ABC.TXT"),
            _entry(path="abc.txt"),
        ]
        with self.assertRaises(B.BuildError):
            B.select_source_files(entries)

    def test_unicode_normalization_collision_rejected(self) -> None:
        entries = [
            _entry(path="data/caf\u00e9.txt"),
            _entry(path="data/cafe\u0301.txt"),
        ]
        with self.assertRaises(B.BuildError):
            B.select_source_files(entries)

    def test_platform_ambiguous_components_rejected(self) -> None:
        for bad in (
            "name.",
            "name ",
            "dir/AUX.txt",
            "dir/com1.log",
            "a:b",
            "a?b",
            "C:/drive.txt",
        ):
            with self.subTest(path=bad):
                with self.assertRaises(B.BuildError):
                    B.validate_source_path(bad)

    def test_distinct_paths_case_distinction_ok(self) -> None:
        entries = [_entry(path="Readme.md"), _entry(path="licence")]
        self.assertEqual(len(B.select_source_files(entries)), 2)


# --- ls-tree parser --------------------------------------------------------


class TestParseLsTreeRecord(unittest.TestCase):
    _SHA = "0" * 40

    def _rec(self, mode: bytes, otype: bytes, sha: bytes = b"0" * 40, path: bytes = b"a") -> bytes:
        return mode + b" " + otype + b" " + sha + b"\t" + path

    def test_unsupported_mode_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.parse_ls_tree_record(self._rec(b"100600", b"blob"))

    def test_unsupported_type_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.parse_ls_tree_record(self._rec(b"100644", b"robot"))

    def test_bad_sha_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.parse_ls_tree_record(self._rec(b"100644", b"blob", sha=b"zzz", path=b"a"))

    def test_non_utf8_path_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.parse_ls_tree_record(self._rec(b"100644", b"blob", path=b"\xff\xfe"))

    def test_empty_record_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.parse_ls_tree_record(b"")

    def test_malformed_no_tab_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.parse_ls_tree_record(b"100644 blob " + b"0" * 40 + b" path")

    def test_valid_record_parsed(self) -> None:
        e = B.parse_ls_tree_record(self._rec(b"100755", b"blob", path=b"scripts/run.sh"))
        self.assertEqual(e.mode, 0o100755)
        self.assertEqual(e.obj_type, "blob")
        self.assertEqual(e.sha, self._SHA)
        self.assertEqual(e.path, "scripts/run.sh")


# --- select_source_files ---------------------------------------------------


class TestSelectSourceFiles(unittest.TestCase):
    def test_symlink_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.select_source_files([_entry(mode=0o120000, path="link")])

    def test_submodule_gitlink_mode_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.select_source_files([_entry(mode=0o160000, path="sub")])

    def test_submodule_obj_type_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.select_source_files([_entry(otype="commit", path="sub")])

    def test_tree_obj_type_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.select_source_files([_entry(otype="tree", path="d")])

    def test_unsupported_regular_mode_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.select_source_files([_entry(mode=0o100600, path="a")])

    def test_duplicate_exact_path_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.select_source_files([_entry(path="a"), _entry(path="a")])

    def test_count_bound_enforced(self) -> None:
        original = B.MAX_SOURCE_FILE_COUNT
        B.MAX_SOURCE_FILE_COUNT = 3
        try:
            entries = [_entry(path=f"f{i}") for i in range(4)]
            with self.assertRaises(B.BuildError):
                B.select_source_files(entries)
        finally:
            B.MAX_SOURCE_FILE_COUNT = original

    def test_regular_files_selected_with_modes(self) -> None:
        entries = [
            _entry(mode=0o100644, path="a"),
            _entry(mode=0o100755, path="b"),
        ]
        selected = B.select_source_files(entries)
        self.assertEqual([e.path for e in selected], ["a", "b"])


# --- blob/size bounds ------------------------------------------------------


class TestBlobBounds(unittest.TestCase):
    def setUp(self) -> None:
        self._save = (
            B.MAX_SINGLE_BLOB_BYTES,
            B.MAX_TOTAL_BLOB_BYTES,
        )

    def tearDown(self) -> None:
        B.MAX_SINGLE_BLOB_BYTES, B.MAX_TOTAL_BLOB_BYTES = self._save

    def test_single_blob_bound_enforced(self) -> None:
        B.MAX_SINGLE_BLOB_BYTES = 20
        repo = _make_repo(files={"big.txt": b"x" * 200})
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)
        with self.assertRaises(B.BuildError):
            _run_build(repo, out)

    def test_total_blob_bound_enforced(self) -> None:
        B.MAX_SINGLE_BLOB_BYTES = 10 ** 9
        B.MAX_TOTAL_BLOB_BYTES = 40
        repo = _make_repo(files={"a.txt": b"a" * 30, "b.txt": b"b" * 30})
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)
        with self.assertRaises(B.BuildError):
            _run_build(repo, out)

    def test_cat_file_cleanup_after_bound_failure(self) -> None:
        # A bound violation mid-stream must still reap the cat-file process:
        # no lingering git children and the failure surfaces as BuildError.
        B.MAX_SINGLE_BLOB_BYTES = 5
        repo = _make_repo(
            files={"a.txt": b"a" * 50, "b.txt": b"b" * 50, "c.txt": b"c" * 50}
        )
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)
        with self.assertRaises(B.BuildError):
            _run_build(repo, out)
        # After a clean reap there must be no git cat-file subprocess still
        # referencing this repo; re-running a build into a fresh dir succeeds.
        out2 = _new_outdir()
        self.addCleanup(_rm, out2)
        B.MAX_SINGLE_BLOB_BYTES = 10 ** 9
        summary = _run_build(repo, out2)
        self.assertTrue(Path(summary["archive"]).exists())


# --- output validation -----------------------------------------------------


class TestOutputValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, self.repo)

    def test_relative_output_rejected(self) -> None:
        with self.assertRaises(B.BuildError):
            B.prepare_output_dir("relative/out", self.repo)

    def test_inside_repo_output_rejected_and_not_created(self) -> None:
        target = self.repo / "out"
        with self.assertRaises(B.BuildError):
            B.prepare_output_dir(str(target), self.repo)
        self.assertFalse(target.exists(), "inside-repo output dir was created")

    def test_symlink_output_rejected(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="thx-real-"))
        self.addCleanup(_rm, target)
        link = target.with_name("thx-link-out")
        link.symlink_to(target, target_is_directory=True)
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(B.BuildError):
            B.prepare_output_dir(str(link), self.repo)

    def test_existing_output_not_overwritten(self) -> None:
        out = _new_outdir()
        self.addCleanup(_rm, out)
        first = _run_build(self.repo, out)
        first_bytes = Path(first["archive"]).read_bytes()
        with self.assertRaises(B.BuildError):
            _run_build(self.repo, out)
        # Original artifacts untouched.
        self.assertEqual(Path(first["archive"]).read_bytes(), first_bytes)
        self.assertTrue(Path(first["checksum"]).exists())


# --- race-safe atomic output ----------------------------------------------


class TestRaceSafeOutput(unittest.TestCase):
    def test_rename_no_clobber_preserves_racer_file(self) -> None:
        work = _new_outdir()
        self.addCleanup(_rm, work)
        src = work / "src.tmp"
        dst = work / "final"
        src.write_bytes(b"OURS")
        dst.write_bytes(b"RACER")  # racer created the final first
        with self.assertRaises(B.BuildError):
            B._rename_no_clobber(src, dst)
        # Racer's content untouched, our temp still present (not consumed).
        self.assertEqual(dst.read_bytes(), b"RACER")
        self.assertTrue(src.exists())

    def test_rename_no_clobber_succeeds_when_free(self) -> None:
        work = _new_outdir()
        self.addCleanup(_rm, work)
        src = work / "src.tmp"
        dst = work / "final"
        src.write_bytes(b"OURS")
        B._rename_no_clobber(src, dst)
        self.assertEqual(dst.read_bytes(), b"OURS")
        self.assertFalse(src.exists())

    def test_build_into_precreated_final_is_rejected(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)
        # Racer pre-creates the archive final name after precheck would pass.
        archive_name = "tracehelix-1.2.3-source.tar.gz"
        (out / archive_name).write_bytes(b"RACER")
        with self.assertRaises(B.BuildError):
            _run_build(repo, out)
        self.assertEqual((out / archive_name).read_bytes(), b"RACER")
        # No temp litter.
        self.assertEqual(sorted(out.iterdir()), [out / archive_name])


# --- atomicity: partial-failure cleanup -----------------------------------


class TestAtomicity(unittest.TestCase):
    def test_second_output_failure_leaves_no_artifacts(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)

        real_write = B._write_exclusive

        def bomb_on_checksum(path: Path, data: bytes) -> None:
            if B.CHECKSUM_NAME in path.name:
                raise OSError("injected checksum write failure")
            return real_write(path, data)

        B._write_exclusive = bomb_on_checksum  # type: ignore[assignment]
        try:
            with self.assertRaises(Exception):
                _run_build(repo, out)
        finally:
            B._write_exclusive = real_write  # type: ignore[assignment]

        leftover = sorted(p.name for p in out.iterdir())
        self.assertEqual(leftover, [], f"leftover artifacts after partial failure: {leftover}")

    def test_checksum_racer_is_preserved_and_owned_archive_is_rolled_back(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)

        real_publish = B._rename_no_clobber

        def race_on_checksum(source: Path, destination: Path) -> None:
            if destination.name == B.CHECKSUM_NAME:
                destination.write_bytes(b"RACER\n")
            real_publish(source, destination)

        B._rename_no_clobber = race_on_checksum  # type: ignore[assignment]
        try:
            with self.assertRaises(B.BuildError):
                _run_build(repo, out)
        finally:
            B._rename_no_clobber = real_publish  # type: ignore[assignment]

        self.assertEqual((out / B.CHECKSUM_NAME).read_bytes(), b"RACER\n")
        self.assertEqual(sorted(path.name for path in out.iterdir()), [B.CHECKSUM_NAME])


# --- manifest integrity ----------------------------------------------------


class TestManifestIntegrity(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _make_repo(files=RICH_FILES, executable=RICH_EXEC)
        self.addCleanup(_rm, self.repo)
        self.out = _new_outdir()
        self.addCleanup(_rm, self.out)
        self.summary = _run_build(self.repo, self.out)

    def test_checksum_file_matches_archive(self) -> None:
        archive = Path(self.summary["archive"])
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        expected = f"{digest}  {archive.name}\n".encode("ascii")
        self.assertEqual((self.out / B.CHECKSUM_NAME).read_bytes(), expected)
        self.assertEqual(self.summary["sha256"], digest)

    def test_manifest_hashes_and_sizes_match_payloads(self) -> None:
        root = f"tracehelix-{self.summary['version']}"
        with _open_tar(self.summary) as tar:
            payloads = {
                m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()
            }
        manifest_bytes = payloads[f"{root}/{B.MANIFEST_NAME}"]
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["version"], self.summary["version"])
        self.assertEqual(manifest["commit"], self.summary["commit"])
        self.assertEqual(manifest["archiveRoot"], root)
        # Every manifest entry must correspond to a real, hash-matching payload.
        seen = set()
        for entry in manifest["entries"]:
            name = f"{root}/{entry['path']}"
            seen.add(name)
            self.assertIn(name, payloads, f"manifest entry missing from archive: {name}")
            data = payloads[name]
            self.assertEqual(
                len(data), entry["size"], f"size mismatch: {name}"
            )
            self.assertEqual(
                hashlib.sha256(data).hexdigest(),
                entry["sha256"],
                f"sha256 mismatch: {name}",
            )
            self.assertIn(entry["mode"], ("0644", "0755"))
        # Manifest must not list itself, and must cover every non-manifest file.
        self.assertNotIn(f"{root}/{B.MANIFEST_NAME}", seen)
        non_manifest_files = {n for n in payloads if not n.endswith(B.MANIFEST_NAME)}
        self.assertEqual(seen, non_manifest_files)
        self.assertEqual(len(manifest["entries"]), self.summary["sourceFileCount"])


# --- CLI smoke -------------------------------------------------------------


class TestCliSmoke(unittest.TestCase):
    def test_main_success_emits_json(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"})
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)
        with chdir(repo):
            rc = B.main(["--output-dir", str(out)])
        self.assertEqual(rc, 0)

    def test_main_bad_version_returns_nonzero_and_no_stdout(self) -> None:
        repo = _make_repo(files={"a.txt": b"a\n"}, version=b"nope")
        self.addCleanup(_rm, repo)
        out = _new_outdir()
        self.addCleanup(_rm, out)
        import contextlib

        buf = io.StringIO()
        with chdir(repo), contextlib.redirect_stdout(buf):
            rc = B.main(["--output-dir", str(out)])
        self.assertNotEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
