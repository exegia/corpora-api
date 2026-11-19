"""Revision-pinned identities for project links and audit records."""

import json
import re
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from corpora_linking import Endpoint, Provenance, Reference
from corpora_linking.models import CitationLocator, StructuralLocator
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpora_api.corpus_hub_read import CorpusHubReadError, _file_path
from corpora_api.references import _selected_corpus


class PinnedAsset(Endpoint):
    """A selected asset in a particular immutable corpus repository revision."""

    @model_validator(mode="after")
    def complete_identity(self) -> Self:
        try:
            corpus = UUID(self.package_id or "")
            if str(corpus) != self.package_id:
                raise ValueError("Corpus UUID must use canonical spelling")
            if not self.revision or not re.fullmatch(r"[0-9a-f]{40}", self.revision):
                raise ValueError("A full corpus revision is required")
            if not self.edition_id or not self.document_id:
                raise ValueError("Edition and asset path are required")
            _file_path(self.document_id)
        except (ValueError, TypeError, CorpusHubReadError, HTTPException) as error:
            raise ValueError(
                "Link endpoint must identify a corpus, revision, asset and selection"
            ) from error
        return self


class LinkEndpoint(PinnedAsset):
    """A complete selection within a revision-pinned asset."""

    @model_validator(mode="after")
    def complete_selection(self) -> Self:
        try:
            corpus = UUID(self.package_id or "")
            if not self.locators or len(self.locators) > 8:
                raise ValueError("One to eight exact locators are required")
            if all(isinstance(locator, CitationLocator) for locator in self.locators):
                raise ValueError("An ambiguous citation alone is not a complete selection")
            for locator in self.locators:
                asset = getattr(locator, "asset_id", None)
                if asset is not None and asset != self.document_id:
                    raise ValueError("Locator and asset path must match")
                if (
                    isinstance(locator, StructuralLocator)
                    and self.document_id == ".corpora/source.corpus"
                ):
                    selected, worker_ref, revision = _selected_corpus(None, locator.anchor_id)
                    if (
                        selected != corpus
                        or revision != self.revision
                        or worker_ref != locator.anchor_id
                    ):
                        raise ValueError("Corpus anchor must identify the exact corpus revision")
        except (ValueError, TypeError, CorpusHubReadError, HTTPException) as error:
            raise ValueError(
                "Link endpoint must identify a corpus, revision, asset and selection"
            ) from error
        return self


class LinkSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_id: UUID
    source: LinkEndpoint
    target: LinkEndpoint
    relationship: str = Field(default="core:cites", min_length=1, max_length=128)
    expected_version: int | None = Field(default=None, strict=True, ge=1, lt=2**63 - 1)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (
            len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"))
            > 32768
        ):
            raise ValueError("Reference exceeds the size limit")
        if not self.reason.strip() or not self.relationship.strip():
            raise ValueError("Reason and relationship cannot be blank")
        return self


class LinkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(strict=True, ge=1, lt=2**63 - 1)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def nonempty_reason(self) -> Self:
        if not self.reason.strip():
            raise ValueError("Reason cannot be blank")
        return self


class LinkRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    actor_id: UUID
    recorded_at: datetime
    reason: str
    action: Literal["edit", "resolve", "approve", "reject", "reopen", "publish", "withdraw"]
    reference: Reference


def draft_reference(body: LinkSave, actor_id: UUID) -> Reference:
    return Reference(
        id=body.reference_id,
        source=body.source,
        target=body.target,
        relationship=body.relationship,
        provenance=Provenance(origin="manual", agent_id=str(actor_id), method="reader-selection"),
    )
