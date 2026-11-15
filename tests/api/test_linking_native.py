"""Native reference selectors preserve source bytes and reject stale assets."""

import io
import zipfile
from xml.etree import ElementTree

import pytest
from corpora_linking import (
    Endpoint,
    Provenance,
    Reference,
    StructuralLocator,
    TextLocator,
    TextSnapshot,
)

from corpora_api.linking_cfi import (
    _dom,
    _package_path,
    _resource,
    make_epub_cfi_selection,
    retrieve_epub_cfi_selection,
)
from corpora_api.linking_cusx import (
    STREAM,
    AnchorBinding,
    bind_cusx_anchors,
    cusx_text,
    retrieve_cusx_selection,
)
from corpora_api.linking_html import extract_html_references_input, retrieve_html_selection
from corpora_api.linking_pdf import sha256_revision
from corpora_api.linking_publication import SnapshotEntry, render_cusx_jump


def test_cusx_jump_encodes_complete_destination_identity():
    source = Endpoint(
        work_id="source",
        edition_id="source-edition",
        package_id="source-package",
        revision="1",
        document_id="source-document",
        locators=(TextLocator(stream_id="body", start=0, end=4, exact="text"),),
    )
    target = Endpoint(
        work_id="work/a",
        edition_id="edition one",
        package_id="shared",
        revision="v1",
        document_id="chapter",
        locators=(StructuralLocator(anchor_id="verse/1"),),
    )
    reference = Reference(
        source=source,
        target=target,
        provenance=Provenance(origin="manual", agent_id="user", method="fixture"),
        review="approved",
        publication="published",
        resolution="resolved",
    )

    def href(value):
        return ElementTree.fromstring(render_cusx_jump(SnapshotEntry(reference=value))).get(
            "link-href"
        )

    first = href(reference)
    assert first == "x-corpora:/work%2Fa/edition%20one/shared/v1/chapter#verse%2F1"
    other_work = reference.model_copy(update={"target": target.model_copy(update={"work_id": "b"})})
    other_edition = reference.model_copy(
        update={"target": target.model_copy(update={"edition_id": "b"})}
    )
    assert href(other_work) != first
    assert href(other_edition) != first
    with pytest.raises(ValueError, match="edition/package/revision/document"):
        href(
            reference.model_copy(
                update={"target": target.model_copy(update={"edition_id": None, "locators": ()})}
            )
        )


def test_html_native_selectors_resolve_exact_text_and_reject_changed_asset():
    data = "<html><body><p>😀 See <em>John</em>.</p></body></html>".encode()
    text = "😀 See John.\n"
    source = Endpoint(
        work_id="w",
        edition_id="e",
        package_id="html",
        revision=sha256_revision(data),
        document_id="d",
    )
    converted = Endpoint(
        work_id="w",
        edition_id="e",
        package_id="text",
        revision=sha256_revision(text.encode()),
        document_id="d",
    )
    conversion = extract_html_references_input(
        data, original=source, converted=converted, asset_id="html"
    )
    from corpora_api.linking_runtime import AssetRecord, AuthorizedInventoryResolver

    asset = AssetRecord(endpoint=source, format="html", data=data, asset_id="html")
    valid = conversion.mappings[0].original
    assert AuthorizedInventoryResolver._native(asset, valid) == valid.locators[0].text.exact
    locator = valid.locators[0].model_copy(update={"asset_id": "forged"})
    forged = valid.model_copy(update={"locators": (locator,)})
    with pytest.raises(ValueError, match="asset ID"):
        AuthorizedInventoryResolver._native(asset, forged)
    with pytest.raises(ValueError, match="asset ID"):
        AuthorizedInventoryResolver._native(
            AssetRecord(endpoint=source, format="html", data=data), valid
        )
    for mapping in conversion.mappings:
        assert (
            retrieve_html_selection(data, mapping.original) == mapping.converted.locators[0].exact
        )
    with pytest.raises(ValueError, match="stale HTML asset"):
        retrieve_html_selection(data + b" ", conversion.mappings[0].original)


def test_cusx_anchor_resolves_cross_node_text_and_rejects_broken_pair():
    data = (
        '<usx version="3.0"><para style="p">😀 See <char style="it">John</char> 3:16.</para></usx>'
    ).encode()
    base = Endpoint(work_id="w", edition_id="e", package_id="p", revision="1", document_id="d")
    snapshot = TextSnapshot(endpoint=base, stream_id=STREAM, text=cusx_text(data))
    quote = "See John 3:16"
    start = snapshot.text.index(quote)
    selection = base.model_copy(
        update={
            "locators": (
                TextLocator(
                    stream_id=STREAM,
                    start=start,
                    end=start + len(quote),
                    exact=quote,
                    prefix="😀 ",
                    suffix=".",
                ),
            )
        }
    )
    output = bind_cusx_anchors(
        data, snapshot, (AnchorBinding(selection=selection, anchor_id="chosen"),)
    )
    target = base.model_copy(update={"locators": (StructuralLocator(anchor_id="chosen"),)})
    assert retrieve_cusx_selection(output, target) == quote
    with pytest.raises(ValueError, match="missing"):
        retrieve_cusx_selection(output.replace(b'eid="chosen"', b'eid="other"'), target)


def test_epub_cfi_resolves_inline_unicode_and_rejects_changed_package():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="EPUB/package.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "EPUB/package.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="first"/></spine></package>',
        )
        archive.writestr(
            "EPUB/first.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>😀 See <em>John</em> 3:16.</p></body></html>",
        )
    data = output.getvalue()
    base = Endpoint(
        work_id="w", edition_id="e", package_id="p", revision=sha256_revision(data), document_id="d"
    )
    _, root = _resource(data, _package_path(data), "first.xhtml")
    text, _ = _dom(root)
    quote = "John 3:16"
    start = text.index(quote)
    endpoint = make_epub_cfi_selection(
        data, base, asset_id="epub", href="first.xhtml", start=start, end=start + len(quote)
    )
    assert retrieve_epub_cfi_selection(data, endpoint) == quote
    with pytest.raises(ValueError, match="pinned"):
        retrieve_epub_cfi_selection(data + b"changed", endpoint)


def test_optional_pdf_selector_preserves_rotated_page_geometry():
    pymupdf = pytest.importorskip("pymupdf")
    from corpora_api.linking_pymupdf import (
        _extract,
        extract_pymupdf_references_input,
        pdf_display_quad_to_native,
        retrieve_pdf_selection,
    )

    with pymupdf.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((70, 90), "John 3:16")
        page.set_cropbox(pymupdf.Rect(30, 20, 280, 190))
        page.set_rotation(90)
        data = document.tobytes()
    text, glyphs = _extract(data)
    original = Endpoint(
        work_id="w",
        edition_id="e",
        package_id="pdf",
        revision=sha256_revision(data),
        document_id="d",
    )
    converted = Endpoint(
        work_id="w",
        edition_id="e",
        package_id="text",
        revision=sha256_revision(text.encode()),
        document_id="d",
    )
    conversion = extract_pymupdf_references_input(
        data, original=original, converted=converted, asset_id="pdf"
    )
    assert text == "John 3:16"
    for mapping in conversion.mappings:
        assert retrieve_pdf_selection(data, mapping.original) == mapping.converted.locators[0].exact
    with pymupdf.open(stream=data, filetype="pdf") as document:
        quad = glyphs[0][3]
        rotated = [
            pymupdf.Point(point.x, point.y) * document[0].rotation_matrix for point in quad.points
        ]
    transformed = pdf_display_quad_to_native(
        data, page=1, points=tuple((point.x, point.y) for point in rotated)
    )
    assert all(
        abs(a.x - b.x) < 1e-5 and abs(a.y - b.y) < 1e-5
        for a, b in zip(transformed.points, quad.points, strict=True)
    )
