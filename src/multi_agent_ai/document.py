"""Header-block documents: the common grammar of docs/protocol/formats.md section 1.

Reads are lenient (BOM, CRLF, header-termination warnings, duplicate keys); writes are strict
(UTF-8 without BOM, LF, header block, one blank line, body, final newline) and atomic
(temporary file in the same directory, then rename).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

BOM = "﻿"
_HEADER_LINE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")

REPLACE_ATTEMPTS = 8
"""How often a rename is tried when Windows reports the target as locked."""


class DocumentError(ValueError):
    """The file cannot be read as a coordination document at all (e.g. invalid UTF-8)."""


class OwnershipError(PermissionError):
    """A write would create or replace a file that belongs to another author."""


@dataclass
class Document:
    """A parsed file. ``headers`` maps lowercase keys to the first value seen."""

    headers: dict[str, str]
    body: str
    warnings: list[str] = field(default_factory=list)
    repeated: dict[str, list[str]] = field(default_factory=dict)
    """Values of repeated keys after the first (the first wins)."""

    def get(self, key: str) -> str | None:
        return self.headers.get(key)


def parse_bytes(data: bytes) -> Document:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError(f"invalid UTF-8 at byte {exc.start}") from None
    return parse_text(text)


def parse_text(text: str) -> Document:
    if text.startswith(BOM):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the final newline does not create an extra line

    headers: dict[str, str] = {}
    repeated: dict[str, list[str]] = {}
    warnings: list[str] = []
    body_start = len(lines)
    blank_found = False
    for number, line in enumerate(lines):
        if not line.strip():
            body_start = number + 1
            blank_found = True
            break
        match = _HEADER_LINE.fullmatch(line)
        if not match:
            warnings.append(
                f"line {number + 1} is not 'key: value'; header ends there, the rest is body"
            )
            body_start = number
            break
        key, value = match.group(1).lower(), match.group(2).rstrip()
        if key in headers:
            repeated.setdefault(key, []).append(value)
            warnings.append(f"repeated key '{key}' on line {number + 1}; the first value wins")
        else:
            headers[key] = value
    if not blank_found and all(line.strip() for line in lines):
        warnings.append("missing blank separator after the header block")
    body = "\n".join(lines[body_start:]).rstrip("\n")
    return Document(headers=headers, body=body, warnings=warnings, repeated=repeated)


def read_document(path: Path) -> Document:
    return parse_bytes(Path(path).read_bytes())


def render(headers: list[tuple[str, str]], body: str = "") -> str:
    """Render a document in the canonical written form. Raises ValueError on bad input."""
    out: list[str] = []
    for key, value in headers:
        if not _KEY.fullmatch(key) or key != key.lower():
            raise ValueError(f"invalid header key {key!r} (lowercase key expected)")
        if "\n" in value or "\r" in value:
            raise ValueError(f"header {key!r} value must be a single line")
        out.append(f"{key}: {value.strip()}\n")
    out.append("\n")
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if body:
        out.append(body + "\n")
    return "".join(out)


def atomic_write(path: Path, text: str) -> None:
    """Write UTF-8 (no BOM) via a temporary file in the same directory, then rename.

    Windows can refuse the rename while another process holds the target open; the rename is
    retried a bounded number of times before the PermissionError is raised.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(min(0.05 * 2**attempt, 1.0))
    finally:
        if temp.exists():
            temp.unlink()
