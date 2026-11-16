"""Verify genuine Git objects and adversarial structural cases."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from corpora_api.corpus_git_tree import (
    CorpusGitFile,
    CorpusGitTreeError,
    verify_corpus_git_file,
    verify_corpus_git_structure,
)


def git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=True,
        timeout=15,
        env=env,
    ).stdout


@pytest.fixture
def real_git_tree(tmp_path):
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "Synthetic Test")
    git(tmp_path, "config", "user.email", "synthetic@example.org")
    (tmp_path / "baseline.txt").write_text("baseline\n")
    git(tmp_path, "add", "baseline.txt")
    git(tmp_path, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD").decode().strip()
    (tmp_path / "baseline.txt").unlink()
    (tmp_path / ".gitattributes").write_text("*.cfr binary\n")
    (tmp_path / "tf/.cfr").mkdir(parents=True)
    (tmp_path / "tf/otype.tf").write_text("@node\n")
    (tmp_path / "tf/.cfr/cache.cfr").write_bytes(b"\0cache\xff")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "candidate")
    candidate = git(tmp_path, "rev-parse", "HEAD").decode().strip()
    root = git(tmp_path, "rev-parse", "HEAD^{tree}").decode().strip()
    commit = git(tmp_path, "cat-file", "commit", candidate)
    tree_ids = {root}
    for line in git(tmp_path, "ls-tree", "-r", "-t", "HEAD").decode().splitlines():
        descriptor, _path = line.split("\t", 1)
        _mode, kind, oid = descriptor.split()
        if kind == "tree":
            tree_ids.add(oid)
    trees = {oid: git(tmp_path, "cat-file", "tree", oid) for oid in tree_ids}
    paths = frozenset({".gitattributes", "tf/otype.tf", "tf/.cfr/cache.cfr"})
    return tmp_path, candidate, baseline, commit, trees, paths


def test_real_git_commit_direct_parent_and_complete_regular_tree(real_git_tree):
    repo, candidate, baseline, commit, trees, paths = real_git_tree
    files = verify_corpus_git_structure(candidate, baseline, commit, trees, paths)
    assert {f.path for f in files} == paths
    assert all(f.mode == "100644" and len(f.blob_sha1) == 40 for f in files)
    for entry in files:
        actual = (repo / entry.path).read_bytes()
        blob = git(repo, "cat-file", "blob", entry.blob_sha1)
        verify_corpus_git_file(entry, blob, actual, len(actual), hashlib.sha256(actual).hexdigest())
        with pytest.raises(CorpusGitTreeError):
            verify_corpus_git_file(
                entry, blob + b"x", actual, len(actual), hashlib.sha256(actual).hexdigest()
            )


def test_real_git_lfs_pointer_and_resolved_content_are_both_verified(real_git_tree):
    repo, *_ = real_git_tree
    payload = b"synthetic LFS payload\x00"
    digest = hashlib.sha256(payload).hexdigest()
    pointer = (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(payload)}\n"
    ).encode()
    oid = git(repo, "hash-object", "-w", "--stdin", data=pointer).decode().strip()
    entry = CorpusGitFile("tf/cache.cfr", "100644", oid)
    verify_corpus_git_file(entry, pointer, payload, len(payload), digest)
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_file(entry, pointer, payload + b"x", len(payload), digest)
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_file(entry, pointer, b"x" * len(payload), len(payload), digest)
    changed_pointer = pointer.replace(
        f"size {len(payload)}".encode(), f"size {len(payload) + 1}".encode()
    )
    changed_oid = git(repo, "hash-object", "-w", "--stdin", data=changed_pointer).decode().strip()
    with pytest.raises(CorpusGitTreeError, match="pointer"):
        verify_corpus_git_file(
            CorpusGitFile("tf/cache.cfr", "100644", changed_oid),
            changed_pointer,
            payload,
            len(payload),
            digest,
        )


def test_wrong_parent_tampered_commit_missing_or_modified_tree_fail(real_git_tree):
    _, candidate, baseline, commit, trees, paths = real_git_tree
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_structure(candidate, "f" * 40, commit, trees, paths)
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_structure(candidate, baseline, commit + b"x", trees, paths)
    missing = dict(trees)
    missing.pop(next(iter(missing)))
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_structure(candidate, baseline, commit, missing, paths)
    corrupted = {key: value + b"x" for key, value in trees.items()}
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_structure(candidate, baseline, commit, corrupted, paths)
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_structure(candidate, baseline, commit, trees, paths - {".gitattributes"})


def test_real_git_symlink_is_rejected(real_git_tree):
    repo, _, baseline, _, _, _ = real_git_tree
    (repo / "link").symlink_to("tf/otype.tf")
    git(repo, "add", "link")
    git(repo, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "symlink")
    candidate = git(repo, "rev-parse", "HEAD").decode().strip()
    parent = git(repo, "rev-parse", "HEAD^").decode().strip()
    root = git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    commit = git(repo, "cat-file", "commit", candidate)
    tree_ids = {root}
    for line in git(repo, "ls-tree", "-r", "-t", "HEAD").decode().splitlines():
        descriptor, _path = line.split("\t", 1)
        _mode, kind, oid = descriptor.split()
        if kind == "tree":
            tree_ids.add(oid)
    trees = {oid: git(repo, "cat-file", "tree", oid) for oid in tree_ids}
    assert parent != baseline
    with pytest.raises(CorpusGitTreeError, match="unsupported file modes"):
        verify_corpus_git_structure(
            candidate,
            parent,
            commit,
            trees,
            frozenset({"link", ".gitattributes", "tf/otype.tf", "tf/.cfr/cache.cfr"}),
        )


def test_git_lfs_pointer_requires_canonical_encoding_and_literal_is_preserved(real_git_tree):
    repo, *_ = real_git_tree
    content = b"payload"
    digest = hashlib.sha256(content).hexdigest()
    pointer = (
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(content)}\n"
    ).encode()
    for malformed in (pointer.replace(b"\n", b"\r\n"), pointer.rstrip(b"\n")):
        oid = git(repo, "hash-object", "-w", "--stdin", data=malformed).decode().strip()
        entry = CorpusGitFile("x", "100644", oid)
        with pytest.raises(CorpusGitTreeError, match="pointer"):
            verify_corpus_git_file(entry, malformed, content, len(content), digest)
    oid = git(repo, "hash-object", "-w", "--stdin", data=pointer).decode().strip()
    verify_corpus_git_file(
        CorpusGitFile("x", "100644", oid),
        pointer,
        pointer,
        len(pointer),
        hashlib.sha256(pointer).hexdigest(),
    )


def test_malformed_commit_parent_after_author_is_rejected(real_git_tree):
    repo, _, baseline, _, trees, paths = real_git_tree
    root = git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    commit = (
        f"tree {root}\nauthor Test <test@example.org> 0 +0000\nparent {baseline}\n"
        "committer Test <test@example.org> 0 +0000\n\nmessage\n"
    ).encode()
    candidate = (
        git(repo, "hash-object", "--literally", "-t", "commit", "-w", "--stdin", data=commit)
        .decode()
        .strip()
    )
    with pytest.raises(CorpusGitTreeError):
        verify_corpus_git_structure(candidate, baseline, commit, trees, paths)


def test_git_rejects_unsorted_tree_and_extra_empty_directory(real_git_tree):
    repo, _, baseline, _, _, _ = real_git_tree
    blob = git(repo, "hash-object", "-w", "--stdin", data=b"x").decode().strip()
    empty = git(repo, "mktree", data=b"").decode().strip()
    cases = [
        (
            b"100644 z\0" + bytes.fromhex(blob) + b"100644 a\0" + bytes.fromhex(blob),
            frozenset({"a", "z"}),
            {},
        ),
        (
            b"40000 empty\0" + bytes.fromhex(empty) + b"100644 file\0" + bytes.fromhex(blob),
            frozenset({"file"}),
            {empty: b""},
        ),
    ]
    for tree, paths, extra in cases:
        root = (
            git(repo, "hash-object", "--literally", "-t", "tree", "-w", "--stdin", data=tree)
            .decode()
            .strip()
        )
        candidate = (
            git(
                repo,
                "-c",
                "commit.gpgSign=false",
                "commit-tree",
                root,
                "-p",
                baseline,
                data=b"test\n",
            )
            .decode()
            .strip()
        )
        commit = git(repo, "cat-file", "commit", candidate)
        with pytest.raises(CorpusGitTreeError):
            verify_corpus_git_structure(candidate, baseline, commit, {root: tree, **extra}, paths)
