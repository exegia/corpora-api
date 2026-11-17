import hashlib
import os
import shutil
import struct
import subprocess
import zlib

import pytest

from corpora_api import corpus_git_pack as parser


def size(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def entry(kind, data, base=None, declared=None):
    length = len(data) if declared is None else declared
    header = bytearray([(kind << 4) | (length & 15)])
    length >>= 4
    while length:
        header[-1] |= 128
        header.append(length & 127)
        length >>= 7
    return bytes(header) + (bytes.fromhex(base) if base else b"") + zlib.compress(data)


def packed(*entries):
    data = b"PACK" + struct.pack(">II", 2, len(entries)) + b"".join(entries)
    return data + hashlib.sha1(data, usedforsecurity=False).digest()


def decode(tmp_path, raw):
    path = tmp_path / "test.pack"
    path.write_bytes(raw)
    return parser.decode_git_pack(path)


def test_forward_ref_delta_and_chain_preserve_type_hash_and_bytes(tmp_path):
    base = parser.GitObject("blob", b"hello")
    middle = parser.GitObject("blob", b"hello!")
    final = parser.GitObject("blob", b"hello! world")
    delta1 = size(5) + size(6) + b"\x90\x05\x01!"
    delta2 = size(6) + size(12) + b"\x90\x06\x06 world"
    objects = decode(
        tmp_path,
        packed(
            entry(7, delta2, middle.oid()),
            entry(7, delta1, base.oid()),
            entry(3, base.data),
        ),
    )
    assert objects == {obj.oid(): obj for obj in (base, middle, final)}


def test_zero_copy_size_means_65536(tmp_path):
    base = parser.GitObject("blob", b"a" * 65536)
    changed = parser.GitObject("blob", base.data + b"!")
    delta = size(65536) + size(65537) + b"\x80\x01!"
    objects = decode(tmp_path, packed(entry(3, base.data), entry(7, delta, base.oid())))
    assert objects[changed.oid()] == changed


@pytest.mark.parametrize("payload", [b"", b"a", b"x" * 200000])
def test_ordinary_object_sizes_and_stream_boundaries(tmp_path, payload):
    obj = parser.GitObject("blob", payload)
    assert decode(tmp_path, packed(entry(3, payload))) == {obj.oid(): obj}


@pytest.mark.parametrize(
    "delta",
    [
        b"\x05\x06\0",  # reserved instruction
        b"\x05\x06\x90",  # truncated copy count
        b"\x05\x06\x91\x04\x03",  # copy beyond base
        b"\x05\x01\x90\x05",  # copy beyond target
        b"\x04\x01\x01x",  # wrong base length
        b"\x05\x02\x01x",  # short result
        b"\x05\x02\x02x",  # truncated literal
        b"\x80" * 8,  # unbounded varint
    ],
)
def test_malformed_delta_is_rejected(tmp_path, delta):
    base = parser.GitObject("blob", b"hello")
    with pytest.raises(parser.CorpusGitPackError):
        decode(tmp_path, packed(entry(3, base.data), entry(7, delta, base.oid())))


@pytest.mark.parametrize(
    "raw",
    [
        packed(entry(3, b"x"))[:-1],
        packed(entry(3, b"x")) + b"trailing",
        packed(entry(0, b"x")),
        packed(entry(5, b"x")),
        packed(entry(6, b"x")),
        packed(entry(3, b"x", declared=0)),
        packed(entry(3, b"x", declared=2)),
        packed(entry(3, b"x")[:-1]),
        packed(entry(3, b"x"), entry(3, b"x")),
        packed(entry(7, b"\x01\x01\x01x", "f" * 40)),
    ],
)
def test_invalid_pack_or_missing_dependency_is_rejected(tmp_path, raw):
    with pytest.raises(parser.CorpusGitPackError):
        decode(tmp_path, raw)


def test_declared_object_count_and_trailing_objects_are_checked(tmp_path):
    raw = packed(entry(3, b"a"), entry(3, b"b"))
    wrong = raw[:8] + struct.pack(">I", 1) + raw[12:-20]
    wrong += hashlib.sha1(wrong, usedforsecurity=False).digest()
    with pytest.raises(parser.CorpusGitPackError):
        decode(tmp_path, wrong)


@pytest.mark.parametrize("limit", ["MAX_INFLATED_BYTES", "MAX_RESOLVED_BYTES"])
def test_cumulative_budget_includes_every_object(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(parser, limit, 9)
    with pytest.raises(parser.CorpusGitPackError):
        decode(tmp_path, packed(entry(3, b"aaaaa"), entry(3, b"bbbbb")))


def test_waiting_deltas_are_charged_before_reconstructing_the_next(tmp_path, monkeypatch):
    base = parser.GitObject("blob", b"hello")
    monkeypatch.setattr(parser, "MAX_RESOLVED_BYTES", 16)
    with pytest.raises(parser.CorpusGitPackError):
        decode(
            tmp_path,
            packed(
                entry(7, b"\x05\x06\x90\x05\x01!", base.oid()),
                entry(7, b"\x05\x06\x90\x05\x01?", base.oid()),
                entry(3, base.data),
            ),
        )


def test_delta_depth_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "MAX_DELTA_DEPTH", 0)
    base = parser.GitObject("blob", b"hello")
    with pytest.raises(parser.CorpusGitPackError):
        decode(tmp_path, packed(entry(3, base.data), entry(7, b"\x05\x01\x01!", base.oid())))


@pytest.mark.skipif(shutil.which("git") is None, reason="Git required for independent pack oracle")
def test_git_generated_pack_matches_git_for_every_object(tmp_path):
    repo = tmp_path / "repo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    def git(*args, input=None):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            env=env,
            input=input,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout

    repo.mkdir()
    git("init", "--template=", "--object-format=sha1")
    for i in range(12):
        (repo / f"text{i}.txt").write_text("source line\n" * 2000 + f"variant {i}\n")
    git("add", ".")
    git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-m", "fixture"
    )
    listed = git("rev-list", "--objects", "--all")
    raw = git("pack-objects", "--stdout", "--window=50", "--depth=10", input=listed)
    objects = decode(tmp_path, raw)
    ids = [line.split(b" ", 1)[0].decode() for line in listed.splitlines()]
    assert set(objects) == set(ids)
    for oid in ids:
        kind = git("cat-file", "-t", oid).decode().strip()
        assert objects[oid] == parser.GitObject(kind, git("cat-file", kind, oid))
