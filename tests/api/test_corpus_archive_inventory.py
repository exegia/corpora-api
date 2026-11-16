from __future__ import annotations

import hashlib
import io
import stat
import struct
import warnings
import zipfile
import zlib

import pytest

from corpora_api import corpus_archive_inventory as inventory


def make_zip(items):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, contents in items:
                archive.writestr(name, contents)
    return data.getvalue()


def test_hashes_all_original_paths_and_bytes_including_cache_and_metadata():
    payloads = {
        "manifest.json": b'{"title":"example"}',
        "tf/otype.tf": b"@node\n",
        "tf/.cfr/cache.cfr": b"\x00\xffcache",
        "images/cover.bin": b"image",
    }
    archive = make_zip(list(reversed(list(payloads.items()))))
    actual = inventory.inventory_corpus_archive(archive)
    assert actual.archive_size == len(archive)
    assert actual.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert [(f.path, f.size, f.sha256) for f in actual.files] == [
        (name, len(data), hashlib.sha256(data).hexdigest())
        for name, data in sorted(payloads.items())
    ]


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute", "a/../b", "a//b", "a/./b", "a\\b", ".git/config", "a/\u0001bad"],
)
def test_rejects_unsafe_paths(path):
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(make_zip([(path, b"x")]))


def test_rejects_duplicate_and_conflicting_paths():
    for items in ([("same", b"a"), ("same", b"b")], [("file", b"a"), ("file/child", b"b")]):
        with pytest.raises(inventory.CorpusInventoryError):
            inventory.inventory_corpus_archive(make_zip(items))


def test_rejects_unix_symlink_and_special_files():
    for kind in (stat.S_IFLNK, stat.S_IFIFO):
        entry = zipfile.ZipInfo("link")
        entry.create_system = 3
        entry.external_attr = (kind | 0o777) << 16
        with pytest.raises(inventory.CorpusInventoryError):
            inventory.inventory_corpus_archive(make_zip([(entry, b"target")]))


def test_directory_entries_are_empty_and_headers_are_consistent():
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(make_zip([("folder/", b"bad"), ("file", b"ok")]))
    archive = bytearray(make_zip([("folder/", b""), ("file", b"ok")]))
    local_filename = 30  # Fixed local header size for the first entry.
    archive[local_filename] = ord("x")
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(archive))


def test_rejects_corrupt_zip_and_bad_crc():
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(b"not a zip")
    archive = bytearray(make_zip([("data", b"unique content")]))
    offset = archive.index(b"PK\x01\x02")
    archive[offset + 16 : offset + 20] = b"\0" * 4
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(archive))


def test_rejects_forged_size_and_crc_that_hide_deflate_output():
    payload = b"x" * 500
    archive = bytearray(make_zip([("data", payload)]))
    central = archive.index(b"PK\x01\x02")
    prefix_crc = zlib.crc32(payload[:5])
    struct.pack_into("<L", archive, 14, prefix_crc)
    struct.pack_into("<L", archive, 22, 5)
    struct.pack_into("<L", archive, central + 16, prefix_crc)
    struct.pack_into("<L", archive, central + 24, 5)
    # Standard zipfile trusts the forged prefix; the inventory must not.
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        assert source.read("data") == payload[:5]
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(archive))


@pytest.mark.parametrize("length", [65535, 65536, 65537, 65539, 131073, 196609])
def test_hashes_deflate_streams_across_chunk_boundaries(length):
    payload = b"\x00" * length
    result = inventory.inventory_corpus_archive(make_zip([("data", payload)]))
    assert result.files[0].size == length
    assert result.files[0].sha256 == hashlib.sha256(payload).hexdigest()


def test_rejects_inconsistent_local_compression_method():
    archive = bytearray(make_zip([("data", b"content")]))
    struct.pack_into("<H", archive, 8, zipfile.ZIP_STORED)
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(archive))


def test_rejects_entries_pointing_to_the_same_local_header():
    archive = bytearray(make_zip([("first", b"a"), ("second", b"b")]))
    first = archive.index(b"PK\x01\x02")
    second = archive.index(b"PK\x01\x02", first + 4)
    first_offset = struct.unpack_from("<L", archive, first + 42)[0]
    struct.pack_into("<L", archive, second + 42, first_offset)
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(archive))


def test_rejects_unverified_compression_methods():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("data", b"content")
    with pytest.raises(inventory.CorpusInventoryError, match="compression"):
        inventory.inventory_corpus_archive(data.getvalue())


def test_valid_data_descriptor_is_supported_and_checked():
    class NonSeekable(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *args):
            raise io.UnsupportedOperation("not seekable")

    data = NonSeekable()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as source:
        source.writestr("data", b"content")
    archive = data.getvalue()
    assert inventory.inventory_corpus_archive(archive).files[0].size == 7
    tampered = bytearray(archive)
    descriptor = tampered.index(b"PK\x07\x08")
    struct.pack_into("<L", tampered, descriptor + 4, 0)
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(tampered))
    inconsistent_local = bytearray(archive)
    struct.pack_into("<L", inconsistent_local, 22, 1)
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(bytes(inconsistent_local))

    # A signatureless descriptor may have a CRC equal to the signature bytes.
    collision = bytes.fromhex("ac0a7ad5")
    assert zlib.crc32(collision) == 0x08074B50
    data = NonSeekable()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as source:
        source.writestr("file", collision)
    signatureless = bytearray(data.getvalue())
    position = signatureless.index(b"PK\x07\x08")
    del signatureless[position : position + 4]
    end = signatureless.index(b"PK\x05\x06")
    directory_offset = struct.unpack_from("<L", signatureless, end + 16)[0]
    struct.pack_into("<L", signatureless, end + 16, directory_offset - 4)
    assert inventory.inventory_corpus_archive(bytes(signatureless)).files[0].size == 4


def test_applies_actual_limits(monkeypatch):
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(b"x" * (inventory.MAX_ARCHIVE_BYTES + 1))
    monkeypatch.setattr(inventory, "MAX_FILES", 1)
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(make_zip([("a", b"a"), ("b", b"b")]))
    monkeypatch.setattr(inventory, "MAX_UNPACKED_BYTES", 4)
    with pytest.raises(inventory.CorpusInventoryError):
        inventory.inventory_corpus_archive(make_zip([("large", b"12345")]))
