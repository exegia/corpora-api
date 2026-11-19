"""Read-only Hub verification before finalizing an uncertain version save."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from huggingface_hub import CommitOperationAdd, HfApi
from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpora_api.corpus_archive_prepare import prepare_corpus_archive
from corpora_api.corpus_creation import CreationRPC
from corpora_api.corpus_git_acquire import download_commit_pack
from corpora_api.corpus_git_pack import decode_git_pack
from corpora_api.corpus_hub_publish import (
    CorpusHubPublisher,
    publication_files,
    verify_downloaded_candidate,
)
from corpora_api.corpus_hub_stage import CorpusHubStager
from corpora_api.validation import ValidationBridge


class SaveReconciliationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready"]
    mode: Literal["save", "import"] = "save"
    corpus_id: UUID
    operation_id: UUID
    claim_id: UUID
    publication_claim: UUID
    repo_id: str
    repo_type: Literal["dataset"]
    expected_sequence: int = Field(strict=True, ge=0, lt=2**63 - 1)
    expected_parent_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size: int = Field(strict=True, gt=0, le=3_000_000)
    stage_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    stage_path: str

    @model_validator(mode="after")
    def matches_claim(self) -> SaveReconciliationTarget:
        organization = os.getenv("HF_CORPUS_ORGANIZATION", "")
        if (
            not organization
            or self.repo_id != f"{organization}/corpus-{self.corpus_id}"
            or self.claim_id == self.publication_claim
            or self.stage_sha == self.expected_parent_sha
            or self.stage_path != f".corpora-staging/{self.operation_id}/{self.claim_id}.corpus"
            or (self.mode == "import") != (self.expected_sequence == 0)
        ):
            raise ValueError("Invalid save reconciliation target")
        return self


def execute_save_reconciliation(target: SaveReconciliationTarget, deadline: float) -> None:
    """Verify the exact private staged bytes, retained ref, full tree and Git parent.

    Never create a ref or commit, reclaim a claim, or infer failure from a missing ref.
    """
    token = os.environ["HF_CORPUS_PROVISION_TOKEN"]
    api = HfApi(endpoint="https://huggingface.co", token=token)
    with httpx.Client(timeout=15, trust_env=False) as client:
        reader = CorpusHubStager(api, client, token)
        publisher = CorpusHubPublisher(api, client, token)
        stage_ref = "corpora-stage-" + str(target.operation_id)
        publisher._check_private_ref(target.repo_id, target.repo_type, stage_ref, target.stage_sha)
        archive = reader._read_verified_bytes(
            target.repo_id,
            target.repo_type,
            target.stage_sha,
            target.stage_path,
            target.archive_size,
            target.input_sha256,
        )
        publisher._check_private_ref(target.repo_id, target.repo_type, stage_ref, target.stage_sha)
        prepared = prepare_corpus_archive(
            "staged.corpus",
            archive,
            target.input_sha256,
            ValidationBridge(),
            expected_format="corpus",
        )
        files = publication_files(prepared)
        ref = "corpora-op-" + str(target.operation_id)
        info = api.repo_info(target.repo_id, repo_type=target.repo_type, revision=ref, token=token)
        sha = info.sha
        if (
            info.id != target.repo_id
            or info.private is not True
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or sha in (target.expected_parent_sha, target.stage_sha)
        ):
            raise ValueError("Save candidate is unavailable")
        publisher._check_private_ref(target.repo_id, target.repo_type, ref, sha)
        with tempfile.TemporaryDirectory(prefix="corpora-reconcile-") as directory:
            pack = Path(directory) / "candidate.pack"
            download_commit_pack(
                target.repo_id,
                target.repo_type,
                "refs/heads/" + ref,
                sha,
                token,
                pack,
                deadline=deadline,
            )
            objects = decode_git_pack(pack)
        verify_downloaded_candidate(
            objects,
            sha,
            target.expected_parent_sha,
            files,
            lambda path, size, digest: reader._read_verified_bytes(
                target.repo_id, target.repo_type, sha, path, size, digest
            ),
        )
        publisher._check_private_ref(target.repo_id, target.repo_type, ref, sha)
        params = {
            "p_operation": str(target.operation_id),
            "p_claim": str(target.claim_id),
            "p_publication": str(target.publication_claim),
            "p_sha": sha,
        }
        rpc = CreationRPC()
        try:
            finish = (
                "corpora_complete_import"
                if target.mode == "import"
                else "corpora_finish_save_reconciliation"
            )
            if rpc.call(finish, params) is not True:
                raise ValueError("Save reconciliation was rejected")
        except Exception:
            find = (
                "corpora_find_import_result"
                if target.mode == "import"
                else "corpora_find_version_save_result"
            )
            if rpc.call(find, params) is not True:
                raise


def resume_initial_import(target: SaveReconciliationTarget, deadline: float) -> None:
    """Retry only an unchanged operation branch with its original Git parent fence."""
    if target.mode != "import" or target.expected_sequence != 0:
        raise ValueError("Only initial imports can be resumed")
    token = os.environ["HF_CORPUS_PROVISION_TOKEN"]
    api = HfApi(endpoint="https://huggingface.co", token=token)
    ref = "corpora-op-" + str(target.operation_id)
    info = api.repo_info(target.repo_id, repo_type=target.repo_type, revision=ref, token=token)
    if info.id != target.repo_id or info.private is not True:
        raise ValueError("Private operation branch unavailable")
    if info.sha == target.expected_parent_sha:
        with httpx.Client(timeout=15, trust_env=False) as client:
            reader = CorpusHubStager(api, client, token)
            publisher = CorpusHubPublisher(api, client, token)
            stage_ref = "corpora-stage-" + str(target.operation_id)
            publisher._check_private_ref(
                target.repo_id, target.repo_type, stage_ref, target.stage_sha
            )
            archive = reader._read_verified_bytes(
                target.repo_id,
                target.repo_type,
                target.stage_sha,
                target.stage_path,
                target.archive_size,
                target.input_sha256,
            )
            publisher._check_private_ref(
                target.repo_id, target.repo_type, stage_ref, target.stage_sha
            )
            prepared = prepare_corpus_archive(
                "staged.corpus",
                archive,
                target.input_sha256,
                ValidationBridge(),
                expected_format="corpus",
            )
            files = publication_files(prepared)
            publisher._check_private_ref(
                target.repo_id, target.repo_type, ref, target.expected_parent_sha
            )
            try:
                api.create_commit(
                    target.repo_id,
                    [
                        CommitOperationAdd(path_in_repo=path, path_or_fileobj=data)
                        for path, data in sorted(files.items())
                    ],
                    commit_message="Store validated Corpora content",
                    repo_type=target.repo_type,
                    revision=ref,
                    parent_commit=target.expected_parent_sha,
                    token=token,
                )
            except Exception:
                # A lost response or a competing fenced commit is not a failure receipt.
                pass
    # Even after a successful write, finalize only after complete read-back verification.
    execute_save_reconciliation(target, deadline)
