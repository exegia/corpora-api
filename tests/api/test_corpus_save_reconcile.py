from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_corpus_hub_publish import candidate, prepared

from corpora_api import corpus_save_reconcile as reconcile
from corpora_api.corpus_git_tree import CorpusGitTreeError
from corpora_api.corpus_hub_publish import publication_files


@pytest.fixture
def verified(monkeypatch):
    corpus, operation, claim, publication = (uuid4() for _ in range(4))
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture")
    monkeypatch.setenv("HF_CORPUS_PROVISION_TOKEN", "fixture-placeholder")
    target = reconcile.SaveReconciliationTarget.model_validate(
        {
            "status": "ready",
            "corpus_id": str(corpus),
            "operation_id": str(operation),
            "claim_id": str(claim),
            "publication_claim": str(publication),
            "repo_id": f"fixture/corpus-{corpus}",
            "repo_type": "dataset",
            "expected_sequence": 1,
            "expected_parent_sha": "a" * 40,
            "input_sha256": "b" * 64,
            "archive_size": 7,
            "stage_sha": "c" * 40,
            "stage_path": f".corpora-staging/{operation}/{claim}.corpus",
        }
    )
    source = prepared()
    objects, sha = candidate(publication_files(source))
    calls = []

    class API:
        endpoint = "https://huggingface.co"

        def repo_info(self, _repo, *, revision, **_kwargs):
            calls.append(("ref", revision))
            return SimpleNamespace(
                id=target.repo_id,
                private=True,
                sha=target.stage_sha if revision.startswith("corpora-stage-") else sha,
            )

    class Reader:
        def __init__(self, *_):
            pass

        def _read_verified_bytes(self, _repo, _kind, rev, path, size, digest):
            calls.append(("read", rev, path, size, digest))
            assert (rev, path, size, digest) == (target.stage_sha, target.stage_path, 7, "b" * 64)
            return b"archive"

    monkeypatch.setattr(reconcile, "HfApi", lambda **_kwargs: API())
    monkeypatch.setattr(reconcile, "CorpusHubStager", Reader)
    monkeypatch.setattr(reconcile, "prepare_corpus_archive", lambda *_a, **_kw: source)
    monkeypatch.setattr(reconcile, "download_commit_pack", lambda *_a, **_kw: None)
    monkeypatch.setattr(reconcile, "decode_git_pack", lambda *_: objects)
    monkeypatch.setattr(
        reconcile.CreationRPC,
        "call",
        lambda _self, name, params: calls.append((name, params)) or True,
    )
    return SimpleNamespace(target=target, source=source, objects=objects, sha=sha, calls=calls)


def test_verified_candidate_can_finalize_without_new_hub_writes(verified):
    reconcile.execute_save_reconciliation(verified.target, 10)
    name, params = verified.calls[-1]
    assert name == "corpora_finish_save_reconciliation"
    assert params == {
        "p_operation": str(verified.target.operation_id),
        "p_claim": str(verified.target.claim_id),
        "p_publication": str(verified.target.publication_claim),
        "p_sha": verified.sha,
    }


def test_initial_import_uses_its_own_verified_finalization(verified):
    value = verified.target.model_dump(mode="json")
    target = reconcile.SaveReconciliationTarget.model_validate(
        {**value, "mode": "import", "expected_sequence": 0}
    )
    reconcile.execute_save_reconciliation(target, 10)
    assert verified.calls[-1][0] == "corpora_complete_import"
    with pytest.raises(ValidationError):
        reconcile.SaveReconciliationTarget.model_validate(
            {**value, "mode": "save", "expected_sequence": 0}
        )


def test_lost_import_ack_checks_only_the_matching_result(verified, monkeypatch):
    value = verified.target.model_dump(mode="json")
    target = reconcile.SaveReconciliationTarget.model_validate(
        {**value, "mode": "import", "expected_sequence": 0}
    )
    calls = []

    def rpc(_self, name, params):
        calls.append((name, params))
        if name == "corpora_complete_import":
            raise TimeoutError("Lost acknowledgement")
        assert name == "corpora_find_import_result" and params == calls[0][1]
        return True

    monkeypatch.setattr(reconcile.CreationRPC, "call", rpc)
    reconcile.execute_save_reconciliation(target, 10)
    assert [name for name, _ in calls] == [
        "corpora_complete_import",
        "corpora_find_import_result",
    ]


def test_wrong_git_parent_never_finalizes(verified, monkeypatch):
    objects, wrong_sha = candidate(publication_files(verified.source), parent="f" * 40)

    class WrongParentAPI:
        endpoint = "https://huggingface.co"

        def repo_info(self, _repo, *, revision, **_kwargs):
            return SimpleNamespace(
                id=verified.target.repo_id,
                private=True,
                sha=verified.target.stage_sha
                if revision.startswith("corpora-stage-")
                else wrong_sha,
            )

    monkeypatch.setattr(reconcile, "HfApi", lambda **_kwargs: WrongParentAPI())
    monkeypatch.setattr(reconcile, "decode_git_pack", lambda *_: objects)
    with pytest.raises(CorpusGitTreeError, match="parent"):
        reconcile.execute_save_reconciliation(verified.target, 10)
    assert not any(item[0] == "corpora_finish_save_reconciliation" for item in verified.calls)


def test_reconciliation_target_rejects_forged_staging_path(verified):
    value = verified.target.model_dump(mode="json")
    with pytest.raises(ValidationError):
        reconcile.SaveReconciliationTarget.model_validate({**value, "stage_path": "other"})


@pytest.mark.parametrize("committed", [True, False])
def test_lost_finalization_ack_only_observes_same_candidate(verified, monkeypatch, committed):
    calls = []

    def rpc(_self, name, params):
        calls.append((name, params))
        if name == "corpora_finish_save_reconciliation":
            raise TimeoutError("Lost acknowledgement")
        assert name == "corpora_find_version_save_result" and params == calls[0][1]
        return committed

    monkeypatch.setattr(reconcile.CreationRPC, "call", rpc)
    if committed:
        reconcile.execute_save_reconciliation(verified.target, 10)
    else:
        with pytest.raises(TimeoutError):
            reconcile.execute_save_reconciliation(verified.target, 10)
    assert [name for name, _params in calls] == [
        "corpora_finish_save_reconciliation",
        "corpora_find_version_save_result",
    ]
