import hashlib
import io
import zipfile

import pytest

from corpora_api.corpus_archive_inventory import inventory_corpus_archive
from corpora_api.corpus_archive_prepare import PreparedCorpusArchive
from corpora_api.corpus_git_pack import GitObject
from corpora_api.corpus_git_tree import CorpusGitTreeError
from corpora_api.corpus_hub_publish import (
    GIT_ATTRIBUTES,
    SOURCE_PATH,
    publication_files,
    verify_downloaded_candidate,
)
from corpora_api.corpus_hub_stage import CorpusStageError


def prepared(extra=None, *, cache=True):
    # Layout unit fixture only: the real core validator is a separate gate.
    files = {
        "tf/otype.tf": b"@node\n@valueType=str\n\n1 word\n",
        "manifest.yml": b"title: Mini\n",
        "annotations.json": b"{}",
    }
    if cache:
        files["tf/.cfr/cache.cfr"] = b"layout placeholder"
    files.update(extra or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, value in files.items():
            archive.writestr(path, value)
    data = out.getvalue()
    return PreparedCorpusArchive("fixture.corpus", data, inventory_corpus_archive(data))


def real_archive(root):
    """Generate a real cache with a slot omitted by the fine passage nodes."""
    from cfabric import Fabric as CF
    from tf.fabric import Fabric as TF

    tf = root / "tf"
    tf.mkdir(parents=True)
    metadata = {
        name: {"valueType": "str"}
        for name in ("otype", "text", "book", "passage", "annotation", "oslots")
    }
    metadata["otext"] = {
        "sectionTypes": "book,passage",
        "sectionFeatures": "book,passage",
        "fmt:text-orig-full": "{text}",
    }
    assert TF(locations=str(tf), silent="deep").save(
        nodeFeatures={
            "otype": {
                1: "word",
                2: "word",
                3: "word",
                4: "word",
                5: "word",
                6: "book",
                7: "passage",
                8: "passage",
            },
            "text": {1: "In ", 2: "the ", 3: "gap ", 4: "and ", 5: "out."},
            "book": {6: "Mini"},
            "passage": {7: "A", 8: "B"},
            "annotation": {1: "opening", 3: "uncovered", 5: "closing"},
        },
        edgeFeatures={"oslots": {6: {1, 2, 3, 4, 5}, 7: {1, 2}, 8: {4, 5}}},
        metaData=metadata,
    )
    cf = CF(locations=str(tf), silent="deep")
    assert cf.loadAll(silent="deep")
    cache = tf / ".cfr" / "tiny.cfr"
    cache.parent.mkdir(exist_ok=True)
    cf.compile(str(cache), silent="deep")
    assert cache.is_file()
    (root / "manifest.yml").write_text('name: Tiny\nversion: "1.0"\nformat: corpus\n')
    (root / "annotations.json").write_text("{}\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_file() and (".cfr" not in path.parts or path == cache):
                archive.write(path, path.relative_to(root).as_posix())
    return out.getvalue()


def test_real_corpus_archive_retains_tf_cache_manifest_and_annotations(tmp_path):
    from corpora_api.corpus_archive_prepare import prepare_corpus_archive
    from corpora_api.validation import ValidationBridge

    data = real_archive(tmp_path)
    ready = prepare_corpus_archive(
        "tiny.corpus",
        data,
        hashlib.sha256(data).hexdigest(),
        ValidationBridge(),
        expected_format="corpus",
    )
    files = publication_files(ready)
    assert files[SOURCE_PATH] == data
    assert files["tf/.cfr/tiny.cfr"] and files["tf/otype.tf"]
    assert files["manifest.yml"] and files["annotations.json"]


def candidate(files, *, parent="a" * 40, lfs=frozenset()):
    objects = {}
    tree = {}
    for path, data in files.items():
        if path in lfs:
            data = (
                b"version https://git-lfs.github.com/spec/v1\noid sha256:"
                + hashlib.sha256(data).hexdigest().encode()
                + b"\nsize "
                + str(len(data)).encode()
                + b"\n"
            )
        obj = GitObject("blob", data)
        objects[obj.oid()] = obj
        directory = tree
        parts = path.split("/")
        for part in parts[:-1]:
            directory = directory.setdefault(part, {})
        directory[parts[-1]] = obj.oid()

    def build(directory):
        rows = []
        for name, value in directory.items():
            is_dir = isinstance(value, dict)
            oid = build(value) if is_dir else value
            mode = b"40000" if is_dir else b"100644"
            rows.append(
                (
                    name.encode() + (b"/" if is_dir else b"\0"),
                    mode + b" " + name.encode() + b"\0" + bytes.fromhex(oid),
                )
            )
        obj = GitObject("tree", b"".join(value for _, value in sorted(rows)))
        objects[obj.oid()] = obj
        return obj.oid()

    root = build(tree)
    commit = GitObject(
        "commit",
        (
            f"tree {root}\nparent {parent}\n"
            "author Fixture <fixture@example.org> 0 +0000\n"
            "committer Fixture <fixture@example.org> 0 +0000\n\nCorpus\n"
        ).encode(),
    )
    objects[commit.oid()] = commit
    return objects, commit.oid()


def test_layout_keeps_original_bytes_and_adds_managed_archive():
    source = prepared({"tf/empty.tf": b""})
    files = publication_files(source)
    assert files[SOURCE_PATH] == source.archive
    assert files[".gitattributes"] == GIT_ATTRIBUTES
    assert files["tf/empty.tf"] == b""
    assert files["manifest.yml"] == b"title: Mini\n"
    assert set(files) == {item.path for item in source.inventory.files} | {
        SOURCE_PATH,
        ".gitattributes",
    }


@pytest.mark.parametrize(
    "path",
    [
        ".corpora/other",
        ".Corpora/source.corpus",
        ".corpora-staging/other",
        ".gitattributes",
        "other/otype.tf",
    ],
)
def test_ambiguous_or_reserved_layout_is_rejected(path):
    with pytest.raises(CorpusStageError):
        publication_files(prepared({path: b"untrusted"}))


def test_initial_import_requires_shipped_cache():
    with pytest.raises(CorpusStageError):
        publication_files(prepared(cache=False))


def test_exact_tree_verifies_empty_files_and_downloads_only_lfs_payloads():
    files = publication_files(prepared({"tf/empty.tf": b""}))
    objects, sha = candidate(files, lfs={SOURCE_PATH})
    reads = []

    def read(path, size, digest):
        reads.append(path)
        assert len(files[path]) == size and hashlib.sha256(files[path]).hexdigest() == digest
        return files[path]

    verify_downloaded_candidate(objects, sha, "a" * 40, files, read)
    assert reads == [SOURCE_PATH]


@pytest.mark.parametrize("change", ["extra", "missing", "bytes", "parent"])
def test_candidate_must_match_all_expected_paths_bytes_and_parent(change):
    expected = publication_files(prepared())
    actual = dict(expected)
    parent = "a" * 40
    if change == "extra":
        actual["private-unexpected.txt"] = b"extra"
    elif change == "missing":
        del actual["manifest.yml"]
    elif change == "bytes":
        actual["manifest.yml"] = b"different"
    else:
        parent = "b" * 40
    objects, sha = candidate(actual, parent=parent)
    with pytest.raises(CorpusGitTreeError):
        verify_downloaded_candidate(
            objects, sha, "a" * 40, expected, lambda *_: pytest.fail("Unexpected download")
        )


def test_lfs_payload_is_verified_independently():
    expected = publication_files(prepared())
    objects, sha = candidate(expected, lfs={SOURCE_PATH})
    with pytest.raises(CorpusGitTreeError):
        verify_downloaded_candidate(objects, sha, "a" * 40, expected, lambda *_: b"wrong")
