"""Turn a drop into documents: loose text files, or one tar/zip archive.

The live path only understands text. That is a real limit of the demo (the
docling step that turns PDF/DOCX into text is designed, not built), so the
corpus loader accepts text and says what it skipped instead of feeding binary
files to the model.

An uploaded archive is untrusted input. Everything here is written against
that assumption: members are read from the archive object, never extracted to
disk, paths are rejected if they could escape a directory, symlinks and
devices are ignored, and every size is checked before the bytes are read.
"""
from __future__ import annotations

import base64
import io
import tarfile
import zipfile
from pathlib import PurePosixPath

MAX_DOCS = 64
MAX_DOC_CHARS = 400_000
MAX_TOTAL_CHARS = 8_000_000
# An archive rides inside the JSON body as base64 text, so it is already in
# memory before any cap runs; the decode would allocate its raw bytes on top.
# This is the upload's own size cap — the ones above see members, not the
# archive — and it is generous over MAX_TOTAL_CHARS because text compresses.
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}


class CorpusError(ValueError):
    """The upload cannot be turned into documents; the message is user-facing."""


def _text_name(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES


def _safe_name(name: str) -> str | None:
    """A member name that can be shown and keyed without escaping anywhere."""
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts:
        return None
    return "/".join(parts)


def _decode(raw: bytes, name: str) -> str:
    text = raw.decode("utf-8", errors="replace")
    # A NUL byte in the first block means it is not a text file, whatever its
    # extension says. Refusing here keeps the model from reading binary noise.
    if "\x00" in text[:2048]:
        raise CorpusError(f"{name} looks binary — only plain text is accepted")
    return text


def decode_archive(data: str) -> bytes:
    """The base64 body of an archive upload, refused on both sides of the decode.

    Base64 grows 4/3, so the encoded text is refused before the raw bytes are
    allocated, and the decoded size is checked again so the cap holds whatever
    the encoding claims. Messages carry "limit" so the server maps them to 413.
    """
    max_chars = (MAX_ARCHIVE_BYTES // 3 + 1) * 4
    if len(data) > max_chars:
        raise CorpusError(f"the archive is {len(data):,} base64 characters; "
                          f"the limit is {max_chars:,}")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as e:
        raise CorpusError("the archive payload is not valid base64") from e
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise CorpusError(f"the archive is {len(raw):,} bytes; the limit is "
                          f"{MAX_ARCHIVE_BYTES:,} bytes")
    return raw


def documents_from_files(files: list[dict]) -> tuple[list[dict], list[str]]:
    """Loose files from the browser's file reader."""
    docs: list[dict] = []
    skipped: list[str] = []
    total = 0
    for item in files:
        name = _safe_name(str(item.get("name") or "document"))
        if name is None or not _text_name(name):
            skipped.append(str(item.get("name") or "unnamed"))
            continue
        text = str(item.get("text") or "")
        if "\x00" in text[:2048]:
            raise CorpusError(f"{name} looks binary — only plain text is accepted")
        if not text.strip():
            skipped.append(name)
            continue
        if len(text) > MAX_DOC_CHARS:
            raise CorpusError(f"{name} is {len(text):,} characters; the limit per "
                              f"document is {MAX_DOC_CHARS:,}")
        total += len(text)
        docs.append({"name": name, "text": text})
    _check_caps(docs, total)
    return docs, skipped


def documents_from_archive(name: str, data: bytes) -> tuple[list[dict], list[str]]:
    """Read the text members of a .tar/.tar.gz/.tgz/.zip without unpacking."""
    lower = name.lower()
    if lower.endswith(".zip"):
        return _from_zip(name, data)
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        return _from_tar(name, data)
    raise CorpusError(f"{name}: expected a .zip, .tar, .tar.gz or .tgz archive")


def _from_zip(name: str, data: bytes) -> tuple[list[dict], list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CorpusError(f"{name} is not a readable zip archive") from e
    docs: list[dict] = []
    skipped: list[str] = []
    total = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            safe = _safe_name(info.filename)
            if safe is None or not _text_name(safe):
                skipped.append(info.filename)
                continue
            if info.file_size > MAX_DOC_CHARS:
                skipped.append(f"{safe} (over {MAX_DOC_CHARS:,} characters)")
                continue
            with archive.open(info) as fh:
                raw = fh.read(MAX_DOC_CHARS + 1)
            if len(raw) > MAX_DOC_CHARS:
                skipped.append(f"{safe} (over {MAX_DOC_CHARS:,} characters)")
                continue
            text = _decode(raw, safe)
            if not text.strip():
                skipped.append(safe)
                continue
            total += len(text)
            docs.append({"name": safe, "text": text})
            if len(docs) > MAX_DOCS:
                raise CorpusError(f"{name} holds more than {MAX_DOCS} documents; "
                                  "split it into smaller batches")
    _check_caps(docs, total)
    return docs, skipped


def _from_tar(name: str, data: bytes) -> tuple[list[dict], list[str]]:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as e:
        raise CorpusError(f"{name} is not a readable tar archive") from e
    docs: list[dict] = []
    skipped: list[str] = []
    total = 0
    with archive:
        for member in archive:
            if not member.isfile() or member.issym() or member.islnk():
                continue
            safe = _safe_name(member.name)
            if safe is None or not _text_name(safe):
                skipped.append(member.name)
                continue
            if member.size > MAX_DOC_CHARS:
                skipped.append(f"{safe} (over {MAX_DOC_CHARS:,} characters)")
                continue
            fh = archive.extractfile(member)
            if fh is None:
                skipped.append(safe)
                continue
            raw = fh.read(MAX_DOC_CHARS + 1)
            if len(raw) > MAX_DOC_CHARS:
                skipped.append(f"{safe} (over {MAX_DOC_CHARS:,} characters)")
                continue
            text = _decode(raw, safe)
            if not text.strip():
                skipped.append(safe)
                continue
            total += len(text)
            docs.append({"name": safe, "text": text})
            if len(docs) > MAX_DOCS:
                raise CorpusError(f"{name} holds more than {MAX_DOCS} documents; "
                                  "split it into smaller batches")
    _check_caps(docs, total)
    return docs, skipped


def _check_caps(docs: list[dict], total: int) -> None:
    if not docs:
        raise CorpusError("no plain-text documents found — accepted: "
                          + ", ".join(sorted(TEXT_SUFFIXES)))
    if len(docs) > MAX_DOCS:
        raise CorpusError(f"{len(docs)} documents found; the limit is {MAX_DOCS}")
    if total > MAX_TOTAL_CHARS:
        raise CorpusError(f"the upload is {total:,} characters; the limit is "
                          f"{MAX_TOTAL_CHARS:,}")
