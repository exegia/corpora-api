"""Unwired staging of validated save bytes in the corpus's private Hub repo.

The input claim and validator are trusted internal dependencies. This adapter
has no database receipt, dispatcher, whole-operation deadline, retry or cleanup.
Any failure after dispatch must be reconciled; an in-memory receipt is not a
persisted acceptance and does not prove Git parentage or content publication.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

import httpx
from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.utils import validate_repo_id

from corpora_api.corpus_archive_prepare import CorpusValidator, prepare_corpus_archive
from corpora_api.corpus_save_claim import ClaimedCorpusSave

_HUB = "https://huggingface.co"
_MAX_REDIRECTS = 4


class CorpusStageError(Exception):
    """Staging was rejected before any remote operation."""


class CorpusStageUncertain(Exception):
    """A remote operation may have succeeded and requires reconciliation."""


@dataclass(frozen=True)
class StagedCorpusArchive:
    corpus_id: UUID
    operation_id: UUID
    claim_id: UUID
    repo_id: str
    repo_type: str
    input_format: str
    archive_size: int
    archive_sha256: str
    requested_base_sha: str
    stage_ref: str
    stage_sha: str
    stage_path: str


def _safe_url(value: str) -> tuple[str, bool]:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co")))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise CorpusStageUncertain("Staging download redirect is unsafe")
    return value, host == "huggingface.co"


class CorpusHubStager:
    """Create an isolated staging branch and verify its immutable archive bytes."""

    def __init__(self, api: HfApi, client: httpx.Client, token: str) -> None:
        self.api, self.client, self._token = api, client, token

    def stage(
        self, claim: ClaimedCorpusSave, filename: str, archive: bytes, validator: CorpusValidator
    ) -> StagedCorpusArchive:
        if (
            not isinstance(claim, ClaimedCorpusSave)
            or not isinstance(self._token, str)
            or not self._token
            or self.api.endpoint != _HUB
        ):
            raise CorpusStageError("Invalid staging claim")
        try:
            if not all(
                isinstance(v, UUID) for v in (claim.corpus_id, claim.operation_id, claim.claim_id)
            ):
                raise ValueError("Invalid identity")
            organization = os.getenv("HF_CORPUS_ORGANIZATION", "")
            if (
                not organization
                or claim.repo_id != f"{organization}/corpus-{claim.corpus_id}"
                or claim.repo_type not in ("dataset", "model")
                or not isinstance(claim.hub_parent_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", claim.hub_parent_sha)
            ):
                raise ValueError("Invalid repository")
            validate_repo_id(claim.repo_id)
        except (ValueError, TypeError) as error:
            raise CorpusStageError("Invalid staging claim") from error
        # The same bytes, digest and archive format are checked before Hub I/O.
        prepared = prepare_corpus_archive(
            filename, archive, claim.input_sha256, validator, expected_format=claim.input_format
        )
        stage_ref = "corpora-stage-" + str(claim.operation_id)
        stage_path = f".corpora-staging/{claim.operation_id}/{claim.claim_id}.{claim.input_format}"
        try:
            info = self.api.repo_info(
                claim.repo_id,
                repo_type=claim.repo_type,
                revision=claim.hub_parent_sha,
                token=self._token,
            )
            if (
                info.id != claim.repo_id
                or info.private is not True
                or info.sha != claim.hub_parent_sha
            ):
                raise ValueError("Repository baseline is not private or does not match")
            self.api.create_branch(
                claim.repo_id,
                branch=stage_ref,
                revision=claim.hub_parent_sha,
                repo_type=claim.repo_type,
                exist_ok=False,
                token=self._token,
            )
            result = self.api.create_commit(
                claim.repo_id,
                [CommitOperationAdd(path_in_repo=stage_path, path_or_fileobj=archive)],
                commit_message="Stage Corpora save input",
                repo_type=claim.repo_type,
                revision=stage_ref,
                parent_commit=claim.hub_parent_sha,
                token=self._token,
            )
            stage_sha = result.oid
            if (
                not isinstance(stage_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", stage_sha)
                or stage_sha == claim.hub_parent_sha
            ):
                raise ValueError("Invalid staged commit")
            info = self.api.repo_info(
                claim.repo_id, repo_type=claim.repo_type, revision=stage_ref, token=self._token
            )
            if info.id != claim.repo_id or info.private is not True or info.sha != stage_sha:
                raise ValueError("Staging ref does not match")
            self._verify_bytes(claim, stage_sha, stage_path, len(archive))
            return StagedCorpusArchive(
                claim.corpus_id,
                claim.operation_id,
                claim.claim_id,
                claim.repo_id,
                claim.repo_type,
                claim.input_format,
                len(archive),
                prepared.inventory.archive_sha256,
                claim.hub_parent_sha,
                stage_ref,
                stage_sha,
                stage_path,
            )
        except Exception:
            # HTTP errors can contain access tokens or signed URLs. Do not expose them.
            raise CorpusStageUncertain("Corpus staging requires reconciliation") from None

    def _verify_bytes(self, claim: ClaimedCorpusSave, sha: str, path: str, size: int) -> None:
        org, repo = claim.repo_id.split("/", 1)
        prefix = "/datasets" if claim.repo_type == "dataset" else ""
        target = (
            f"{_HUB}{prefix}/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/resolve/{sha}/" + "/".join(quote(part, safe="") for part in path.split("/"))
        )
        for attempt in range(_MAX_REDIRECTS + 1):
            target, is_hub = _safe_url(target)
            headers = {"Accept-Encoding": "identity"}
            if is_hub:
                headers["Authorization"] = "Bearer " + self._token
            request = httpx.Request(
                "GET",
                target,
                headers=headers,
                extensions={"timeout": {k: 15.0 for k in ("connect", "read", "write", "pool")}},
            )
            response = self.client.send(request, stream=True, auth=None, follow_redirects=False)
            with closing(response):
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location or attempt == _MAX_REDIRECTS:
                        raise ValueError("Staging redirect failed")
                    target = urljoin(target, location)
                    continue
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ValueError("Staging read failed")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) != size):
                    raise ValueError("Staging read size mismatch")
                digest, count = hashlib.sha256(), 0
                for chunk in response.iter_raw(chunk_size=65536):
                    count += len(chunk)
                    if count > size:
                        raise ValueError("Staging read is too large")
                    digest.update(chunk)
                if count != size or digest.hexdigest() != claim.input_sha256:
                    raise ValueError("Staging read digest mismatch")
                return
        raise ValueError("Staging redirect limit exceeded")
