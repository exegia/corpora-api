"""EPUB CFI DOM ranges with UTF-16 offsets and exact, pinned quote verification.

Supports element steps with optional ID assertions and character offsets. Text
assertions, side bias, temporal/spatial offsets and cross-resource ranges reject.
"""

import io
import posixpath
import re
import stat
import zipfile
from pathlib import PurePosixPath

from corpora_linking import Endpoint, EpubLocator, verify_text_anchor

from corpora_api.project_asset_verify import corpus_asset, sha256_revision
from corpora_api.project_link_models import LinkEndpoint


def utf16_to_scalar(text: str, offset: int) -> int:
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid UTF-16 offset")
    units = 0
    for index, char in enumerate(text):
        if units == offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > offset:
            raise ValueError("UTF-16 offset splits a surrogate pair")
    if units == offset:
        return len(text)
    raise ValueError("UTF-16 offset exceeds text node")


def _check_epub(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 1024:
            raise ValueError("EPUB has too many entries")
        total = 0
        names = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (
                not name
                or name in names
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or "\x00" in name
                or entry.flag_bits & 1
                or stat.S_ISLNK(entry.external_attr >> 16)
                or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or entry.file_size > 16_777_216
            ):
                raise ValueError("Unsafe EPUB entry")
            names.add(name)
            total += entry.file_size
            if total > 67_108_864:
                raise ValueError("EPUB expands beyond the limit")


def retrieve_epub_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    endpoint = LinkEndpoint.model_validate(raw_endpoint)
    if not endpoint.document_id or not endpoint.document_id.lower().endswith(".epub"):
        raise ValueError("EPUB asset path required")
    if len(endpoint.locators) != 1 or not isinstance(endpoint.locators[0], EpubLocator):
        raise ValueError("One exact EPUB selection required")
    data = corpus_asset(archive_bytes, endpoint.document_id)
    _check_epub(data)
    transient = endpoint.model_copy(update={"revision": sha256_revision(data)})
    return {"text": retrieve_epub_cfi_selection(data, transient)}


def verify_epub_in_corpus(archive_bytes: bytes, raw_endpoint: dict) -> dict:
    retrieve_epub_in_corpus(archive_bytes, raw_endpoint)
    return {"verified": True}


def _xml(data):
    from lxml import etree

    root = etree.fromstring(
        data,
        parser=etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, recover=False
        ),
    )
    if root.getroottree().docinfo.doctype:
        raise ValueError("CFI DOM requires XML without DOCTYPE")
    return root


def _read(archive, path):
    if (
        not path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or "\\" in path
    ):
        raise ValueError("unsafe EPUB resource path")
    if archive.namelist().count(path) != 1:
        raise ValueError("missing or ambiguous EPUB resource")
    entry = archive.getinfo(path)
    if entry.file_size > 16_777_216:
        raise ValueError("EPUB resource exceeds the limit")
    with archive.open(entry) as source:
        data = source.read(entry.file_size + 1)
    if len(data) != entry.file_size:
        raise ValueError("EPUB resource size mismatch")
    return data


def _resource(data: bytes, package_path: str, href: str):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        container = _xml(_read(archive, "META-INF/container.xml"))
        roots = container.xpath("//*[local-name()='rootfile']/@full-path")
        if len(roots) != 1 or roots[0] != package_path:
            raise ValueError("ambiguous or stale EPUB package")
        package = _xml(_read(archive, package_path))
        root = _xml(_read(archive, posixpath.join(posixpath.dirname(package_path), href)))
        return package, root


def _package_path(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        roots = _xml(_read(archive, "META-INF/container.xml")).xpath(
            "//*[local-name()='rootfile']/@full-path"
        )
        if len(roots) != 1:
            raise ValueError("EPUB CFI requires one package rootfile")
        return roots[0]


def _elements(node):
    return [child for child in node if isinstance(child.tag, str)]


def _walk(root, path):
    current = root
    for step, assertion in path:
        if step % 2 or step < 2:
            raise ValueError("CFI element path requires positive even steps")
        children = _elements(current)
        index = step // 2 - 1
        if index >= len(children):
            raise ValueError("stale CFI element step")
        current = children[index]
        if assertion is not None and current.get("id") != assertion:
            raise ValueError("stale CFI ID assertion")
    return current


def _parse_steps(value):
    tokens = re.findall(r"/([1-9][0-9]*)(?:\[([A-Za-z0-9_.:-]+)\])?", value)
    reconstructed = "".join(
        "/" + step + ("[" + identifier + "]" if identifier else "") for step, identifier in tokens
    )
    if reconstructed != value or not tokens:
        raise ValueError("unsupported CFI steps/assertions")
    return [(int(step), identifier or None) for step, identifier in tokens]


def _dom(root):
    # XML comments do not change CFI element numbering. Adjacent text separated
    # only by comments is one CFI character-data slot.
    nodes = []
    offset = 0

    def visit(node, path):
        nonlocal offset
        slots = [node.text or ""]
        elements = _elements(node)
        for child in node:
            if isinstance(child.tag, str):
                slots.append(child.tail or "")
            else:
                slots[-1] += child.tail or ""
        for index, slot in enumerate(slots):
            if slot:
                nodes.append(((*path, 2 * index + 1), offset, slot))
                offset += len(slot)
            if index < len(elements):
                visit(elements[index], (*path, 2 * (index + 1)))

    visit(root, ())
    return "".join(quote for path, start, quote in nodes), nodes


def _point(root, nodes, steps, utf16):
    if not steps or steps[-1][0] % 2 == 0 or steps[-1][1] is not None:
        raise ValueError("CFI range must end in a character-data step")
    _walk(root, steps[:-1])
    path = tuple(step for step, assertion in steps)
    matches = [(start, quote) for candidate, start, quote in nodes if candidate == path]
    if len(matches) != 1:
        raise ValueError("CFI text slot unavailable")
    start, quote = matches[0]
    return start + utf16_to_scalar(quote, utf16)


def _spine(package, package_steps, href):
    itemref = _walk(package, package_steps)
    if (
        itemref.tag.rsplit("}", 1)[-1] != "itemref"
        or itemref.getparent().tag.rsplit("}", 1)[-1] != "spine"
    ):
        raise ValueError("CFI package component must select a spine itemref")
    manifest = package.xpath("//*[local-name()='manifest']/*[@id=$id]", id=itemref.get("idref"))
    if (
        len(manifest) != 1
        or manifest[0].get("href") != href
        or manifest[0].get("media-type") != "application/xhtml+xml"
    ):
        raise ValueError("CFI spine/href mismatch")


def retrieve_epub_cfi_selection(data: bytes, endpoint: Endpoint) -> str:
    if (
        endpoint.revision != sha256_revision(data)
        or len(endpoint.locators) != 1
        or not isinstance(endpoint.locators[0], EpubLocator)
    ):
        raise ValueError("EPUB CFI requires a pinned asset")
    locator = endpoint.locators[0]
    if (
        not locator.cfi.startswith("epubcfi(")
        or not locator.cfi.endswith(")")
        or locator.cfi.count("!") != 1
    ):
        raise ValueError("unsupported EPUB CFI")
    package_part, content = locator.cfi[8:-1].split("!")
    parts = content.split(",")
    if len(parts) != 3:
        raise ValueError("EPUB exact selection requires a CFI range")
    package, root = _resource(data, _package_path(data), locator.href)
    _spine(package, _parse_steps(package_part), locator.href)
    base = _parse_steps(parts[0]) if parts[0] else []
    text, nodes = _dom(root)
    bounds = []
    for point in parts[1:]:
        match = re.fullmatch(r"(.+):([0-9]+)", point)
        if match is None:
            raise ValueError("unsupported CFI range offset/assertion")
        bounds.append(_point(root, nodes, base + _parse_steps(match[1]), int(match[2])))
    selector = locator.text
    if (
        selector is None
        or selector.stream_id != "epub-cfi-dom"
        or selector.normalization != "preserve"
        or (selector.start, selector.end) != tuple(bounds)
    ):
        raise ValueError("CFI range requires matching DOM scalar text evidence")
    if (
        verify_text_anchor(
            selector,
            text,
            expected_revision=endpoint.revision or "",
            actual_revision=sha256_revision(data),
        )
        != "valid"
    ):
        raise ValueError("stale EPUB CFI quote/context")
    return text[selector.start : selector.end]
