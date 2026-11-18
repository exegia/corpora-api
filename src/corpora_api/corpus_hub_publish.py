"""Publish and independently verify one internally claimed initial corpus.

The caller must persist staging and claim publication before calling this
module, and run it in a bounded worker. A returned attestation is created only
after downloading and verifying the exact retained Git tree and file bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

from corpora_api.corpus_archive_prepare import PreparedCorpusArchive
from corpora_api.corpus_git_acquire import download_commit_pack
from corpora_api.corpus_git_pack import GitObject, decode_git_pack
from corpora_api.corpus_git_tree import verify_corpus_git_file, verify_corpus_git_structure
from corpora_api.corpus_hub_stage import CorpusHubStager, CorpusStageError, CorpusStageUncertain
from corpora_api.corpus_publication_claim import CorpusPublicationClaim
from corpora_api.corpus_save_finalize import TrustedCandidateAttestation

SOURCE_PATH = ".corpora/source.corpus"
GIT_ATTRIBUTES = (
    b"*.cfr filter=lfs diff=lfs merge=lfs -text\n*.corpus filter=lfs diff=lfs merge=lfs -text\n"
)


def publication_files(prepared: PreparedCorpusArchive) -> dict[str, bytes]:
    """Keep original archive paths and bytes, with two reserved managed files."""
    if not isinstance(prepared, PreparedCorpusArchive) or not prepared.filename.lower().endswith(
        ".corpus"
    ):
        raise CorpusStageError("Initial import requires a validated .corpus archive")
    entries = prepared.inventory.files
    roots = [str(Path(item.path).parent) for item in entries if Path(item.path).name == "otype.tf"]
    if len(roots) != 1:
        raise CorpusStageError("Corpus must contain exactly one Text-Fabric root")
    prefix = "" if roots[0] == "." else roots[0] + "/"
    caches = [
        item
        for item in entries
        if item.path.startswith(prefix + ".cfr/") and item.path.endswith(".cfr")
    ]
    if len(caches) != 1:
        raise CorpusStageError("Corpus must retain its validated Context-Fabric cache")
    files = {}
    with zipfile.ZipFile(io.BytesIO(prepared.archive)) as archive:
        for entry in entries:
            if entry.path.split("/", 1)[0].casefold() in {
                ".corpora",
                ".corpora-staging",
                ".gitattributes",
            }:
                raise CorpusStageError("Corpus uses a reserved storage path")
            with archive.open(entry.path) as source:
                data = source.read(entry.size + 1)
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise CorpusStageError("Corpus input changed after validation")
            files[entry.path] = data
    if (
        len(prepared.archive) != prepared.inventory.archive_size
        or hashlib.sha256(prepared.archive).hexdigest() != prepared.inventory.archive_sha256
    ):
        raise CorpusStageError("Corpus archive changed after validation")
    return {**files, SOURCE_PATH: prepared.archive, ".gitattributes": GIT_ATTRIBUTES}


def verify_downloaded_candidate(
    objects: dict[str, GitObject],
    sha: str,
    parent: str,
    expected: dict[str, bytes],
    read_lfs: Callable[[str, int, str], bytes],
) -> None:
    """Verify complete tree and every payload, downloading only genuine LFS pointers."""
    commit = objects.get(sha)
    if commit is None or commit.kind != "commit":
        raise CorpusStageUncertain("Published corpus commit is missing")
    entries = verify_corpus_git_structure(
        sha,
        parent,
        commit.data,
        {oid: obj.data for oid, obj in objects.items() if obj.kind == "tree"},
        frozenset(expected),
    )
    for entry in entries:
        obj = objects.get(entry.blob_sha1)
        if obj is None or obj.kind != "blob":
            raise CorpusStageUncertain("Published corpus file is missing")
        wanted = expected[entry.path]
        digest = hashlib.sha256(wanted).hexdigest()
        resolved = obj.data
        if resolved != wanted:
            # Authenticate the exact pointer before making any extra request.
            verify_corpus_git_file(entry, obj.data, wanted, len(wanted), digest)
            resolved = read_lfs(entry.path, len(wanted), digest)
        verify_corpus_git_file(entry, obj.data, resolved, len(wanted), digest)


class CorpusHubPublisher:
    def __init__(self, api: HfApi, client: httpx.Client, token: str) -> None:
        self.api, self.client, self.token = api, client, token

    def publish(
        self,
        claim: CorpusPublicationClaim,
        prepared: PreparedCorpusArchive,
        *,
        deadline: float,
        previous: PreparedCorpusArchive | None = None,
    ) -> TrustedCandidateAttestation:
        stage = claim.stage
        if (
            self.api.endpoint != "https://huggingface.co"
            or not self.token
            or stage.repo_id
            != f"{os.getenv('HF_CORPUS_ORGANIZATION', '')}/corpus-{stage.corpus_id}"
            or not os.getenv("HF_CORPUS_ORGANIZATION")
            or stage.input_format != "corpus"
            or stage.archive_sha256 != prepared.inventory.archive_sha256
            or stage.archive_size != prepared.inventory.archive_size
        ):
            raise CorpusStageError("Invalid corpus publication claim")
        files = publication_files(prepared)
        previous_files = publication_files(previous) if previous is not None else {}
        removed = sorted(previous_files.keys() - files.keys())
        ref = "corpora-op-" + str(stage.operation_id)
        try:
            baseline = self.api.repo_info(
                stage.repo_id,
                repo_type=stage.repo_type,
                revision=stage.requested_base_sha,
                token=self.token,
            )
            if (
                baseline.id != stage.repo_id
                or baseline.private is not True
                or baseline.sha != stage.requested_base_sha
            ):
                raise ValueError("Private baseline changed")
            self.api.create_branch(
                stage.repo_id,
                branch=ref,
                revision=stage.requested_base_sha,
                repo_type=stage.repo_type,
                exist_ok=False,
                token=self.token,
            )
            result = self.api.create_commit(
                stage.repo_id,
                [
                    CommitOperationAdd(path_in_repo=path, path_or_fileobj=data)
                    for path, data in sorted(files.items())
                ]
                + [CommitOperationDelete(path_in_repo=path) for path in removed],
                commit_message="Store validated Corpora content",
                repo_type=stage.repo_type,
                revision=ref,
                parent_commit=stage.requested_base_sha,
                token=self.token,
            )
            sha = result.oid
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("Invalid candidate")
            self._check_private_ref(stage.repo_id, stage.repo_type, ref, sha)
            with tempfile.TemporaryDirectory(prefix="corpora-candidate-") as directory:
                pack = Path(directory) / "candidate.pack"
                download_commit_pack(
                    stage.repo_id,
                    stage.repo_type,
                    "refs/heads/" + ref,
                    sha,
                    self.token,
                    pack,
                    deadline=deadline,
                )
                objects = decode_git_pack(pack)
            reader = CorpusHubStager(self.api, self.client, self.token)
            verify_downloaded_candidate(
                objects,
                sha,
                stage.requested_base_sha,
                files,
                lambda path, size, digest: reader._read_verified_bytes(
                    stage.repo_id, stage.repo_type, sha, path, size, digest
                ),
            )
            self._check_private_ref(stage.repo_id, stage.repo_type, ref, sha)
            return TrustedCandidateAttestation(
                operation_id=stage.operation_id,
                claim_id=stage.claim_id,
                publication_claim=claim.publication_claim,
                corpus_id=stage.corpus_id,
                repo_id=stage.repo_id,
                repo_type=stage.repo_type,
                parent_sha=stage.requested_base_sha if previous is not None else None,
                hub_parent_sha=stage.requested_base_sha,
                commit_sha=sha,
                input_sha256=stage.archive_sha256,
                input_format="corpus",
                retained_ref=ref,
            )
        except Exception:
            raise CorpusStageUncertain("Corpus publication requires reconciliation") from None

    def _check_private_ref(self, repo: str, kind: str, ref: str, sha: str) -> None:
        info = self.api.repo_info(repo, repo_type=kind, revision=ref, token=self.token)
        if info.id != repo or info.private is not True or info.sha != sha:
            raise ValueError("Private retained reference changed")
