"""Bounded browser capture transport; native locators remain owned by corpora-linking."""

import json
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpora_api.project_link_models import LinkEndpoint, PinnedAsset

NodePath = Annotated[
    tuple[Annotated[int, Field(strict=True, ge=0)], ...], Field(min_length=1, max_length=128)
]
NodeText = Annotated[str, Field(min_length=1, max_length=350_000)]


class BrowserSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original: PinnedAsset
    captured_nodes: Annotated[
        tuple[tuple[NodePath, NodeText], ...], Field(min_length=1, max_length=10_000)
    ]
    start_path: NodePath
    start_utf16: int = Field(strict=True, ge=0, le=350_000)
    end_path: NodePath
    end_utf16: int = Field(strict=True, ge=0, le=350_000)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.original.locators:
            raise ValueError("Original must identify a whole HTML asset")
        if not (self.original.document_id or "").lower().endswith((".html", ".htm")):
            raise ValueError("A UTF-8 HTML asset is required")
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > 350_000:
            raise ValueError("Browser capture exceeds the size limit")
        return self


class BrowserSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: LinkEndpoint
    text: str = Field(min_length=1, max_length=32768)
