"""The corpus loader reads untrusted archives without unpacking them."""
from __future__ import annotations

import base64
import io
import tarfile
import zipfile

import pytest

from hybrid_demo.ui import corpus


def _tar(members: list[tuple[str, bytes, str]]) -> bytes:
    """(name, bytes, kind) where kind is "file", "sym" or "dir"."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tf.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def test_tar_members_are_read_in_place_and_paths_cannot_escape():
    data = _tar([
        ("contracts/a.txt", b"termination for convenience", "file"),
        ("../../etc/escape.txt", b"nope", "file"),
        ("/absolute.txt", b"nope", "file"),
        ("link.txt", b"", "sym"),
        ("sub/", b"", "dir"),
        ("image.png", b"\x89PNG", "file"),
    ])

    docs, skipped = corpus.documents_from_archive("corpus.tar.gz", data)

    assert [d["name"] for d in docs] == ["contracts/a.txt"]
    assert docs[0]["text"].startswith("termination")
    # Every rejected member is named, so the presenter can say what was ignored.
    assert "../../etc/escape.txt" in skipped
    assert "/absolute.txt" in skipped
    assert "image.png" in skipped
    assert "link.txt" not in [d["name"] for d in docs]


def test_zip_is_read_the_same_way():
    data = _zip([("b.md", b"# heading"), ("../up.txt", b"nope")])

    docs, skipped = corpus.documents_from_archive("corpus.zip", data)

    assert [d["name"] for d in docs] == ["b.md"]
    assert skipped == ["../up.txt"]


def test_an_oversize_member_is_skipped_not_loaded():
    data = _zip([("huge.txt", b"x" * (corpus.MAX_DOC_CHARS + 10)),
                 ("small.txt", b"fine")])

    docs, skipped = corpus.documents_from_archive("c.zip", data)

    assert [d["name"] for d in docs] == ["small.txt"]
    assert any(name.startswith("huge.txt") and "over" in name for name in skipped)


def test_binary_content_is_refused_even_with_a_text_suffix():
    data = _zip([("not-really.txt", b"\x00\x01\x02\x03")])

    with pytest.raises(corpus.CorpusError, match="binary"):
        corpus.documents_from_archive("c.zip", data)


def test_an_archive_with_no_text_says_so():
    data = _zip([("plot.png", b"\x89PNG")])

    with pytest.raises(corpus.CorpusError, match="no plain-text documents"):
        corpus.documents_from_archive("c.zip", data)


def test_an_unknown_archive_type_is_refused():
    with pytest.raises(corpus.CorpusError, match="expected a"):
        corpus.documents_from_archive("corpus.rar", b"whatever")


def test_loose_files_skip_non_text_and_empty_ones():
    docs, skipped = corpus.documents_from_files([
        {"name": "one.txt", "text": "first"},
        {"name": "two.PDF", "text": "not really a pdf"},
        {"name": "three.md", "text": "   "},
        {"name": "four.txt", "text": "second"},
    ])

    assert [d["name"] for d in docs] == ["one.txt", "four.txt"]
    assert skipped == ["two.PDF", "three.md"]


# --- archive upload size ---------------------------------------------------
# The base64 body is already in memory when it reaches the loader; the caps
# below keep the decode from allocating a second, larger copy of it.

def test_decode_archive_returns_the_raw_bytes():
    raw = b"PK\x03\x04not really a zip"
    assert corpus.decode_archive(base64.b64encode(raw).decode()) == raw


def test_decode_archive_refuses_the_encoded_size_before_decoding(monkeypatch):
    monkeypatch.setattr(corpus, "MAX_ARCHIVE_BYTES", 1024)
    # "A" is valid base64, so only the pre-decode cap can refuse this.
    with pytest.raises(corpus.CorpusError, match="base64 characters"):
        corpus.decode_archive("A" * 2000)


def test_decode_archive_refuses_the_decoded_size(monkeypatch):
    monkeypatch.setattr(corpus, "MAX_ARCHIVE_BYTES", 8)
    with pytest.raises(corpus.CorpusError, match="bytes"):
        corpus.decode_archive(base64.b64encode(b"123456789").decode())


def test_decode_archive_rejects_non_base64():
    with pytest.raises(corpus.CorpusError, match="not valid base64"):
        corpus.decode_archive("this is not base64 !!!")
