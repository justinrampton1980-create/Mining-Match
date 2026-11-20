"""Mining Match - storing uploaded files, and checking what they really are.

LocalStorage keeps files in a folder on the server's disk, which suits development and a
single server. For production, put an S3 (or Azure Blob) class with the same four methods
in its place, in an Australian region. Storage keys come from access_control.add_document,
never from the user.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Optional

PDF, PNG, JPEG = "application/pdf", "image/png", "image/jpeg"
DOC = "application/msword"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def sniff_type(data: bytes) -> Optional[str]:
    """Work out the file type from its content, ignoring the name and what the browser claims.

    Returns one of the accepted types, or None if the file is none of them.
    """
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # old Office format
        return DOC
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None
        if "word/document.xml" in names and "[Content_Types].xml" in names:
            return DOCX
    return None


class LocalStorage:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("bad storage key")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
