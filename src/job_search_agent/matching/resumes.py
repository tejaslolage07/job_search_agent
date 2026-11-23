"""Resume files for phase 2 of the matcher: read lazily (only when a phase-1 verdict is ambiguous) and cached by file hash."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..store import now_iso


def read_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


@dataclass
class ResumeVariant:
    name: str
    path: Path
    file_hash: str
    text: str


class ResumeLibrary:
    """Lazy: nothing is opened until `variants()` is first called."""

    def __init__(self, paths: dict[str, str], conn: sqlite3.Connection | None = None, base_dir: Path | None = None,
                 pdf_reader: Callable[[Path], str] = read_pdf_text) -> None:
        self._paths = paths
        self._conn = conn
        self._base = base_dir or Path.cwd()
        self._reader = pdf_reader
        self._variants: list[ResumeVariant] | None = None
        self.load_count = 0  # test hook: proves jobs decided in phase 1 never read the resume

    @property
    def loaded(self) -> bool:
        return self._variants is not None

    def variants(self) -> list[ResumeVariant]:
        if self._variants is not None:
            return self._variants
        self.load_count += 1
        out = []
        for name, p in self._paths.items():
            path = Path(p).expanduser()
            if not path.is_absolute():
                path = (self._base / path).resolve()
            if not path.exists():
                raise FileNotFoundError(f"resume variant '{name}' not found at {path}")
            fh = hashlib.sha256(path.read_bytes()).hexdigest()
            text = None
            if self._conn is not None:
                row = self._conn.execute("SELECT text FROM resume_cache WHERE file_hash=?", (fh,)).fetchone()
                text = row["text"] if row else None
            if text is None:
                text = self._reader(path)
                if self._conn is not None:
                    self._conn.execute("INSERT OR REPLACE INTO resume_cache VALUES(?,?,?)", (fh, text, now_iso()))
                    self._conn.commit()
            out.append(ResumeVariant(name, path, fh, text))
        self._variants = out
        return out
