"""Configured resource authority and production linking service composition.

Inventory values come from a trusted server manifest/provider, never request bodies.
No schema provisioning, distribution, network lookup or implicit grants occur here.
"""

from typing import Literal
from uuid import UUID

import psycopg
from corpora_linking import (
    BibleBook,
    CatalogEntry,
    Endpoint,
    EpubLocator,
    HtmlRangeLocator,
    HtmlTextLocator,
    PassageEntry,
    PdfLocator,
    ResolutionResult,
    SnapshotCatalog,
    SnapshotResolver,
    StructuralLocator,
    TextSnapshot,
)
from corpora_linking.models import NonEmpty, Value
from pydantic import ConfigDict, model_validator

from .linking_access import endpoint_scope
from .linking_postgres import PostgreSQLReferenceStore


class AssetRecord(Value):
    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )
    endpoint: Endpoint
    format: Literal["pdf", "epub", "html", "cusx"]
    data: bytes
    checksum: NonEmpty | None = None
    asset_id: NonEmpty | None = None

    @model_validator(mode="after")
    def pinned(self) -> "AssetRecord":
        from .linking_pdf import sha256_revision

        if (
            self.endpoint.locators
            or not all(
                getattr(self.endpoint, field)
                for field in ("edition_id", "package_id", "revision", "document_id")
            )
            or (self.format != "cusx" and self.endpoint.revision != sha256_revision(self.data))
            or (self.format == "cusx" and self.checksum != sha256_revision(self.data))
        ):
            raise ValueError("inventory asset requires complete identity and matching SHA-256")
        return self


class EntitlementRecord(Value):
    space_id: UUID
    user_id: UUID
    provider_id: NonEmpty
    provider_revision: NonEmpty
    resources: tuple[Endpoint, ...]


class BibleDetectionContext(Value):
    books: tuple[BibleBook, ...]
    profile: NonEmpty
    scheme_id: NonEmpty
    scheme_version: NonEmpty
    detector_revision: NonEmpty


class LinkingInventory(Value):
    entitlements: tuple[EntitlementRecord, ...] = ()
    bible: BibleDetectionContext | None = None
    catalog: tuple[CatalogEntry, ...] = ()
    passages: tuple[PassageEntry, ...] = ()
    snapshots: tuple[TextSnapshot, ...] = ()
    assets: tuple[AssetRecord, ...] = ()


class LinkingRuntime:
    """Server-configured authority; never accepts an inventory from a client."""

    def __init__(self, *, dsn: str, inventory: LinkingInventory):
        self.dsn = dsn
        self.inventory = LinkingInventory.model_validate(inventory.model_dump())

    def store(self, space_id: UUID, token: str) -> PostgreSQLReferenceStore:
        return PostgreSQLReferenceStore(self.dsn, space_id=space_id, token=token)

    def resolver(self, store: PostgreSQLReferenceStore) -> "AuthorizedInventoryResolver":
        return AuthorizedInventoryResolver(store, self.inventory)


class AuthorizedInventoryResolver:
    """Check grants before asset/snapshot retrieval; preserve stale/unavailable outcomes."""

    def __init__(self, store: PostgreSQLReferenceStore, inventory: LinkingInventory):
        self.store = store
        self.inventory = inventory
        self.base = SnapshotResolver(
            catalog=SnapshotCatalog(entries=inventory.catalog),
            passages=inventory.passages,
            snapshots=inventory.snapshots,
        )

    def resolve(self, request: Endpoint) -> ResolutionResult:
        with psycopg.connect(self.store.dsn) as db:
            actor = self.store._authorize(db, None)
            self.store._check_access(db, actor, request)
            # Passage mappings may point into another pinned edition. Authorize
            # each matching candidate before the base resolver examines its text.
            for passage in self.inventory.passages:
                if (
                    passage.citation in request.locators
                    and passage.target.work_id == request.work_id
                ):
                    self.store._check_access(db, actor, passage.target)
            native = request.locators and all(
                isinstance(
                    locator,
                    (PdfLocator, EpubLocator, HtmlTextLocator, HtmlRangeLocator, StructuralLocator),
                )
                for locator in request.locators
            )
            if not native:
                result = self.base.resolve(request)
                self.store._check_access(db, actor, result)
                return result
            scope = endpoint_scope(request)[0]
            assets = [
                asset
                for asset in self.inventory.assets
                if endpoint_scope(asset.endpoint)[0] == scope
            ]
            if len(assets) != 1:
                return ResolutionResult(
                    status="unavailable", diagnostics=("native asset unavailable or ambiguous",)
                )
            try:
                self._native(assets[0], request)
            except (ValueError, IndexError) as exc:
                return ResolutionResult(
                    status="unresolved",
                    diagnostics=("stale or unsupported native selection", type(exc).__name__),
                )
            return ResolutionResult(status="resolved", candidates=(request,))

    @staticmethod
    def _native(asset: AssetRecord, request: Endpoint) -> str:
        # Native locator IDs describe a server-owned asset, not a caller label.
        identified = (PdfLocator, EpubLocator, HtmlTextLocator, HtmlRangeLocator)
        for locator in request.locators:
            if isinstance(locator, identified) and (
                asset.asset_id is None or locator.asset_id != asset.asset_id
            ):
                raise ValueError("native asset ID does not match trusted inventory")
        if asset.format == "cusx":
            from .linking_cusx import retrieve_cusx_selection

            return retrieve_cusx_selection(asset.data, request)
        if asset.format == "pdf":
            from .linking_pymupdf import retrieve_pdf_selection

            return retrieve_pdf_selection(asset.data, request)
        if asset.format == "epub":
            from .linking_cfi import retrieve_epub_cfi_selection

            return retrieve_epub_cfi_selection(asset.data, request)
        if len(request.locators) == 1 and isinstance(request.locators[0], HtmlRangeLocator):
            from .linking_html import retrieve_html_range

            return retrieve_html_range(asset.data, request)
        from .linking_html import retrieve_html_selection

        return retrieve_html_selection(asset.data, request)

    def retrieve(self, request: Endpoint) -> str:
        result = self.resolve(request)
        if result.status != "resolved" or result.candidates != (request,):
            raise ValueError("selection is stale, ambiguous or unavailable")
        with psycopg.connect(self.store.dsn) as db:
            actor = self.store._authorize(db, None)
            self.store._check_access(db, actor, request)
            from corpora_linking import TextLocator

            if len(request.locators) == 1 and isinstance(request.locators[0], TextLocator):
                locator = request.locators[0]
                snapshots = [
                    snapshot
                    for snapshot in self.inventory.snapshots
                    if endpoint_scope(snapshot.endpoint)[0] == endpoint_scope(request)[0]
                    and snapshot.stream_id == locator.stream_id
                ]
                if len(snapshots) != 1:
                    raise ValueError("selection snapshot ambiguous")
                return snapshots[0].text[locator.start : locator.end]
            assets = [
                asset
                for asset in self.inventory.assets
                if endpoint_scope(asset.endpoint)[0] == endpoint_scope(request)[0]
            ]
            if len(assets) != 1:
                raise ValueError("selection asset unavailable")
            return self._native(assets[0], request)
