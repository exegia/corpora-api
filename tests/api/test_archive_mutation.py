"""Real archive edit, cold Text-Fabric rebuild, and warm mapped cache checks."""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from cfabric import Fabric as ContextFabric
from corpus_validate import read_archive, validate_archive
from corpus_validate.ai_scope import scope_content_hash, scope_nodes
from corpus_validate.ai_types import NodeScope, ScopeLevel
from tf.fabric import Fabric

from corpora_api.archive_mutation import edit_archive
from corpora_api.drafts import DraftHead
from corpora_api.models import AIChatDiffRow, AIChatSuggestion
from corpora_api.mutation_drafts import ArchiveMutationDrafts
from corpora_api.mutation_journal import CurationError, Evidence, Plan


@pytest.fixture
def archive(tmp_path: Path):
    root = tmp_path / "source"
    corpus = root / "corpora"
    corpus.mkdir(parents=True)
    assert Fabric(locations=str(corpus), silent="deep").save(
        nodeFeatures={
            "otype": {1: "word", 2: "word", 3: "book"},
            "word": {1: "Alpha ", 2: "beta"},
            "alternate": {1: "Alpha ", 2: "different"},
            "lemma": {1: "first", 2: "second"},
            "book": {3: "Mini"},
        },
        edgeFeatures={"oslots": {3: {1, 2}}},
        metaData={
            "otype": {"valueType": "str"},
            "oslots": {"valueType": "str"},
            "word": {"valueType": "str"},
            "alternate": {"valueType": "str"},
            "lemma": {"valueType": "str"},
            "book": {"valueType": "str"},
            "otext": {
                "fmt:text-orig-full": "{word}",
                "sectionTypes": "book",
                "sectionFeatures": "book",
            },
        },
    )
    api = ContextFabric(locations=str(corpus), silent="deep").loadAll(silent="deep")
    assert api
    content_hash = scope_content_hash(
        api,
        scope_nodes(
            api,
            NodeScope(
                corpus="draft", level=ScopeLevel.word, node_id=1, label="word", version="1.0"
            ),
        ),
    )
    (root / "manifest.yml").write_text("name: mini\nformat: corpus\nversion: '1.0'\n")
    (root / "toc.yml").write_text("uid: mini\nversion: '1.0'\ndocuments: []\n")
    (root / "history.yml").write_text(
        "versions:\n  - id: v1.0\n    label: v1.0\n    current: true\n"
    )
    (root / "source.xml").write_text("<source>original</source>")
    path = tmp_path / "source.corpus"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("assets/keep-empty/", b"")
        for p in root.rglob("*"):
            if p.is_file():
                out.write(p, p.relative_to(root))
    owner, document, revision = (str(uuid4()) for _ in range(3))
    head = DraftHead(
        document_id=document,
        owner=owner,
        corpus=f"draft:{document}",
        bucket="corpus-ai-drafts",
        object_key=f"{owner}/ai/{document}/{revision}.corpus",
        revision=revision,
        digest="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        version="1.0",
    )
    suggestion = AIChatSuggestion(
        id="suggestion",
        scope={
            "corpus": head.corpus,
            "level": "word",
            "node_id": 1,
            "label": "word",
            "version": "1.0",
            "content_hash": content_hash,
        },
        kind="annotation",
        target_node=1,
        diff=[{"field": "lemma", "old": "first", "new": "changed"}],
        rationale="repair",
        base_version="1.0",
        content_hash=content_hash,
    )
    return path, head, suggestion


def test_edit_rebuilds_cfr_and_preserves_untouched_content(archive, tmp_path):
    path, head, suggestion = archive
    output = tmp_path / "after.corpus"
    plan = edit_archive(path, output, head, suggestion)
    assert plan.after.values["lemma"] == "changed"
    assert plan.after.version == "v1.1" and plan.before.content_hash == plan.after.content_hash
    assert read_archive(output.read_bytes(), "node", node=1)["features"]["lemma"] == "changed"
    assert read_archive(output.read_bytes(), "node", node=2)["features"]["lemma"] == "second"
    with zipfile.ZipFile(output) as result:
        assert result.read("source.xml") == b"<source>original</source>"
        assert result.getinfo("assets/keep-empty/").is_dir()
        history = yaml.safe_load(result.read("history.yml"))["versions"]
        assert history[0]["snapshot_revision"] == str(head.revision)
        assert history[-1]["snapshot_revision"] == plan.after.revision
        assert (
            len(
                [
                    n
                    for n in result.namelist()
                    if n.startswith("corpora/.cfr/") and n.endswith(".cfr")
                ]
            )
            == 1
        )


def test_edit_rejects_history_with_a_different_current_revision(archive, tmp_path):
    path, head, suggestion = archive
    changed = tmp_path / "conflicting.corpus"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(changed, "w") as out:
        for item in source.infolist():
            if item.filename == "history.yml":
                out.writestr(
                    item,
                    yaml.safe_dump(
                        {
                            "versions": [
                                {"id": "v1.0", "current": True, "snapshot_revision": str(uuid4())}
                            ]
                        }
                    ),
                )
            else:
                out.writestr(item, source.read(item))
    conflicting_head = head.model_copy(
        update={"digest": "sha256:" + hashlib.sha256(changed.read_bytes()).hexdigest()}
    )
    with pytest.raises(CurationError, match="history revision does not match"):
        edit_archive(changed, tmp_path / "invalid.corpus", conflicting_head, suggestion)


@pytest.mark.parametrize(
    ("value_size", "message"),
    [(3_950_000, "recovery limit"), (2_000_000, "response limit")],
)
def test_oversized_change_fails_before_staging(archive, monkeypatch, value_size, message):
    _, head, suggestion = archive
    old = "x" * value_size
    suggestion = suggestion.model_copy(
        update={"diff": [AIChatDiffRow(field="lemma", old=old, new="small")]}
    )
    before = Evidence(
        revision=str(head.revision),
        digest=head.digest,
        version=head.version,
        content_hash=suggestion.content_hash,
        values={"lemma": old},
    )
    after = before.model_copy(
        update={
            "revision": str(uuid4()),
            "digest": "sha256:" + "b" * 64,
            "version": "v1.1",
            "values": {"lemma": "small"},
        }
    )
    plan = Plan(before=before, after=after)
    staged = []
    storage = SimpleNamespace(
        resolve=lambda *_: head,
        download=lambda *_: b"archive",
        stage=lambda *args: staged.append(args),
    )
    drafts = ArchiveMutationDrafts(storage)
    monkeypatch.setattr(
        drafts,
        "_worker",
        lambda *_: {"plan": plan.model_dump(mode="json"), "data": "YQ=="},
    )
    with pytest.raises(CurationError, match=message) as caught:
        drafts.prepare(str(head.owner), suggestion)
    assert caught.value.status == 413 and not staged


def test_real_large_feature_is_rejected_before_staging(archive, tmp_path):
    source, head, suggestion = archive
    root = tmp_path / "large"
    with zipfile.ZipFile(source) as original:
        original.extractall(root)
    tf_root = root / "corpora"
    for cache in (".tf", ".cfr", ".cfm"):
        shutil.rmtree(tf_root / cache, ignore_errors=True)
    large_value = "x" * 3_950_000
    assert Fabric(locations=str(tf_root), silent="deep").save(
        nodeFeatures={"lemma": {1: large_value, 2: "second"}},
        metaData={"lemma": {"valueType": "str"}},
    )
    assert ContextFabric(locations=str(tf_root), silent="deep").loadAll(silent="deep")
    target = tmp_path / "large.corpus"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
        for item in root.rglob("*"):
            if item.is_file():
                output.write(item, item.relative_to(root))
    data = target.read_bytes()
    assert len(data) < 3_000_000 and validate_archive(data, "large.corpus")["valid"]
    head = head.model_copy(update={"digest": "sha256:" + hashlib.sha256(data).hexdigest()})
    suggestion = suggestion.model_copy(
        update={"diff": [AIChatDiffRow(field="lemma", old=large_value, new="small")]}
    )
    staged = []
    storage = SimpleNamespace(
        resolve=lambda *_: head,
        download=lambda *_: data,
        stage=lambda *args: staged.append(args),
    )
    with pytest.raises(CurationError, match="recovery limit") as caught:
        ArchiveMutationDrafts(storage).prepare(str(head.owner), suggestion)
    assert caught.value.status == 413 and not staged


@pytest.mark.skipif(
    not os.getenv("CORPORA_CORE_TEST_ROOT"), reason="set CORPORA_CORE_TEST_ROOT for cross-repo test"
)
def test_ai_archive_restores_with_core_version_transformer(archive, tmp_path, monkeypatch):
    import corpus_validate

    core_package = Path(os.environ["CORPORA_CORE_TEST_ROOT"]) / "packages/corpus-validate/src"
    monkeypatch.setattr(corpus_validate, "__path__", [str(core_package / "corpus_validate")])
    prepare_restore = importlib.import_module("corpus_validate.version_edit").prepare_restore
    source, head, suggestion = archive
    output = tmp_path / "updated.corpus"
    plan = edit_archive(source, output, head, suggestion)
    restored = prepare_restore(
        output.read_bytes(),
        source.read_bytes(),
        name="draft.corpus",
        expected_digest=plan.after.digest,
        selected_digest=head.digest,
        expected_version=plan.after.version,
        selected_version=head.version,
        owner=str(head.owner),
        current_revision=plan.after.revision,
        selected_revision=str(head.revision),
        new_revision=str(uuid4()),
    )
    assert restored.version == "v1.2"
    assert read_archive(restored.archive, "node", node=1)["features"]["lemma"] == "first"


def test_edit_rejects_source_text_and_stale_values(archive, tmp_path):
    path, head, suggestion = archive
    for changed in (
        suggestion.model_copy(update={"kind": "text"}),
        suggestion.model_copy(update={"base_version": "stale"}),
    ):
        with pytest.raises(CurationError):
            edit_archive(path, tmp_path / "bad.corpus", head, changed)


def test_stale_cache_cannot_hide_displaced_source_value(archive, tmp_path):
    path, head, suggestion = archive
    root = tmp_path / "stale"
    with zipfile.ZipFile(path) as source:
        source.extractall(root)
    feature = root / "corpora" / "lemma.tf"
    text = feature.read_text()
    assert "first" in text
    feature.write_text(text.replace("first", "different", 1))
    changed = tmp_path / "stale.corpus"
    with zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as output:
        for member in root.rglob("*"):
            if member.is_file():
                output.write(member, member.relative_to(root))
    stale_head = head.model_copy(
        update={"digest": "sha256:" + hashlib.sha256(changed.read_bytes()).hexdigest()}
    )
    with pytest.raises(CurationError, match="Shipped cache does not match"):
        edit_archive(changed, tmp_path / "invalid.corpus", stale_head, suggestion)


def test_missing_history_fails_closed(archive, tmp_path):
    path, head, suggestion = archive
    changed = tmp_path / "missing.corpus"
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for item in source.infolist():
            if item.filename != "history.yml":
                out.writestr(item, source.read(item))
    missing_head = head.model_copy(
        update={"digest": "sha256:" + hashlib.sha256(changed.read_bytes()).hexdigest()}
    )
    with pytest.raises(CurationError):
        edit_archive(changed, tmp_path / "invalid.corpus", missing_head, suggestion)


def test_archive_edit_runs_in_isolated_worker(archive):
    import base64

    from corpora_api.corpus_process import run_corpus

    path, head, suggestion = archive
    result = run_corpus(
        path.read_bytes(),
        "mutation_edit",
        head=head.model_dump(mode="json"),
        suggestion=suggestion.model_dump(mode="json"),
    )
    output = base64.b64decode(result["data"], validate=True)
    assert result["plan"]["after"]["values"]["lemma"] == "changed"
    assert read_archive(output, "node", node=1)["features"]["lemma"] == "changed"


def test_stale_cache_cannot_hide_rendering_config_drift(archive, tmp_path):
    path, head, suggestion = archive
    root = tmp_path / "stale-render"
    with zipfile.ZipFile(path) as source:
        source.extractall(root)
    otext = root / "corpora" / "otext.tf"
    metadata = otext.read_text()
    assert "{word}" in metadata
    otext.write_text(metadata.replace("{word}", "{alternate}"))
    changed = tmp_path / "stale-render.corpus"
    with zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as out:
        for member in root.rglob("*"):
            if member.is_file():
                out.write(member, member.relative_to(root))
    stale_head = head.model_copy(
        update={"digest": "sha256:" + hashlib.sha256(changed.read_bytes()).hexdigest()}
    )
    # Both formats render the same selected first node; another node differs.
    with pytest.raises(CurationError, match="Shipped cache does not match"):
        edit_archive(changed, tmp_path / "invalid.corpus", stale_head, suggestion)


def test_edit_section_label_updates_section_reference_without_changing_text(archive, tmp_path):
    path, head, suggestion = archive
    root = tmp_path / "label-input"
    with zipfile.ZipFile(path) as source:
        source.extractall(root)
    api = ContextFabric(locations=str(root / "corpora"), silent="deep").loadAll(silent="deep")
    selected = NodeScope(
        corpus=head.corpus, level=ScopeLevel.section, node_id=3, label="Mini", version="1.0"
    )
    content_hash = scope_content_hash(api, scope_nodes(api, selected))
    value = suggestion.model_dump(mode="json")
    value.update(
        {
            "scope": {
                "corpus": head.corpus,
                "level": "section",
                "node_id": 3,
                "label": "Mini",
                "version": "1.0",
                "content_hash": content_hash,
            },
            "kind": "label",
            "target_node": 3,
            "diff": [{"field": "book", "old": "Mini", "new": "Revised"}],
            "content_hash": content_hash,
        }
    )
    label_change = AIChatSuggestion.model_validate(value)
    output = tmp_path / "label.corpus"
    plan = edit_archive(path, output, head, label_change)
    assert plan.after.values["book"] == "Revised"
    assert plan.after.content_hash == content_hash
    assert read_archive(output.read_bytes(), "node", node=3)["features"]["book"] == "Revised"


@pytest.mark.parametrize(
    "history",
    [
        "versions:\n  - id: v1.0\n    label: v1.0\n    current: false\n",
        (
            "versions:\n  - id: v1.0\n    label: v1.0\n    current: true\n"
            "  - id: v0.9\n    label: v0.9\n    current: true\n"
        ),
        "versions:\n  - id: v2.0\n    label: v2.0\n    current: true\n",
        (
            "versions:\n  - id: v1.0\n    label: v1.0\n    current: true\n"
            "  - id: v0.9\n    current: 'yes'\n"
        ),
    ],
)
def test_inconsistent_current_history_is_rejected(archive, tmp_path, history):
    path, head, suggestion = archive
    modified = tmp_path / "bad-history.corpus"
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(modified, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for item in source.infolist():
            out.writestr(
                item, history.encode() if item.filename == "history.yml" else source.read(item)
            )
    changed_head = head.model_copy(
        update={"digest": "sha256:" + hashlib.sha256(modified.read_bytes()).hexdigest()}
    )
    with pytest.raises(CurationError, match="current version history"):
        edit_archive(modified, tmp_path / "bad-output.corpus", changed_head, suggestion)


def test_duplicate_history_id_is_rejected_even_if_label_differs(archive, tmp_path):
    path, head, suggestion = archive
    changed = tmp_path / "duplicate-history.corpus"
    history = (
        b"versions:\n  - id: v1.0\n    label: v1.0\n    current: true\n"
        b"  - id: v1.1\n    label: custom\n    current: false\n"
    )
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for item in source.infolist():
            out.writestr(item, history if item.filename == "history.yml" else source.read(item))
    changed_head = head.model_copy(
        update={"digest": "sha256:" + hashlib.sha256(changed.read_bytes()).hexdigest()}
    )
    with pytest.raises(CurationError, match="history already contains"):
        edit_archive(changed, tmp_path / "bad-output.corpus", changed_head, suggestion)
