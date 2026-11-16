"""Unwired private Hub repository provisioning for a reserved corpus.

The caller must establish a live session and ensure request-wide cancellation
before using this adapter. It deliberately does not mark metadata ready.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import httpx

from corpora_api.corpus_metadata import CorpusMetadataStore

_HUB = "https://huggingface.co"


class CorpusHubProvisionError(Exception):
    """The Hub repository could not safely be provisioned."""


class CorpusHubProvisionConflict(CorpusHubProvisionError):
    """The Hub reported a creation conflict that needs review."""


class CorpusHubProvisionUncertain(CorpusHubProvisionError):
    """The remote write may have succeeded and requires reconciliation."""


@dataclass(frozen=True)
class VerifiedPrivateRepository:
    """An observed private repository, not a durable readiness transition."""

    corpus_id: UUID
    metadata_revision: int
    repo_id: str
    repo_type: Literal["dataset", "model"]


class CorpusHubProvisioner:
    """Create one private repository without adopting an existing name."""

    def __init__(self, metadata: CorpusMetadataStore, client: httpx.Client | None = None) -> None:
        self.metadata = metadata
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)

    def create_private(self, verified_user: str, corpus_id: str) -> VerifiedPrivateRepository:
        """Create and observe a private repository; never delete or adopt on failure."""
        record = self.metadata.get(verified_user, corpus_id)
        if record.state != "provisioning":
            raise CorpusHubProvisionError("Corpus is not awaiting repository provisioning")
        # get() verifies the owner and the configured organization and UUID binding.
        self.metadata._check_link(record)
        token = os.getenv("HF_CORPUS_PROVISION_TOKEN", "")
        if not token:
            raise CorpusHubProvisionError("Corpus repository provisioning is unavailable")
        organization, name = record.hf_repo_id.split("/", 1)
        headers = {"Authorization": "Bearer " + token}
        try:
            organization_response = self.client.get(
                _HUB + "/api/organizations/" + quote(organization, safe="") + "/overview",
                headers=headers,
                timeout=15,
                follow_redirects=False,
            )
            if organization_response.status_code != 200:
                raise ValueError("Organization lookup failed")
            overview = organization_response.json()
            if not isinstance(overview, dict) or overview.get("name") != organization:
                raise ValueError("Organization identity mismatch")
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise CorpusHubProvisionError("Corpus organization could not be verified") from error
        payload = {
            "name": name,
            "organization": organization,
            "type": record.hf_repo_type,
            "visibility": "private",
        }
        try:
            created = self.client.post(
                _HUB + "/api/repos/create",
                headers=headers,
                json=payload,
                timeout=15,
                follow_redirects=False,
            )
        except httpx.RequestError as error:
            raise CorpusHubProvisionUncertain("Repository creation needs reconciliation") from error
        if created.status_code == 409:
            raise CorpusHubProvisionConflict("Repository creation conflicts; review is required")
        if created.status_code in (401, 403):
            raise CorpusHubProvisionError("Repository creation was rejected")
        if not 200 <= created.status_code < 300:
            raise CorpusHubProvisionUncertain("Repository creation needs reconciliation")

        prefix = "/datasets/" if record.hf_repo_type == "dataset" else "/"
        expected_url = _HUB + prefix + record.hf_repo_id
        try:
            value = created.json()
            if not isinstance(value, dict) or value.get("url") != expected_url:
                raise ValueError("Unexpected repository URL")
        except (ValueError, TypeError) as error:
            raise CorpusHubProvisionUncertain("Repository creation needs reconciliation") from error

        kind = "datasets" if record.hf_repo_type == "dataset" else "models"
        path = f"/api/{kind}/{quote(organization, safe='')}/{quote(name, safe='')}"
        try:
            observed = self.client.get(
                _HUB + path, headers=headers, timeout=15, follow_redirects=False
            )
            if observed.status_code != 200:
                raise ValueError("Repository inspection failed")
            info = observed.json()
            if not isinstance(info, dict) or info.get("id") != record.hf_repo_id:
                raise ValueError("Repository identity mismatch")
            if info.get("private") is not True:
                raise ValueError("Repository is not confirmed private")
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise CorpusHubProvisionUncertain(
                "Repository verification needs reconciliation"
            ) from error
        return VerifiedPrivateRepository(
            corpus_id=record.id,
            metadata_revision=record.revision,
            repo_id=record.hf_repo_id,
            repo_type=record.hf_repo_type,
        )
