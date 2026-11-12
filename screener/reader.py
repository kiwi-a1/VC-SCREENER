"""Step 3, Read. Turns attachments and public Google links into page-numbered text.

Only PDF, PowerPoint (.pptx) and Word (.docx) are opened. Anything else, a locked
link, or an unreadable file goes to `not_read` with the reason. Text is extracted
locally (so white-on-white hidden text is caught by the injection scan too).
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .models import Attachment, Email
from .rails import MAX_FILES, MAX_PAGES, READABLE_EXT

GOOGLE_LINK = re.compile(r"https://docs\.google\.com/(document|presentation)/d/([\w-]+)")
URL = re.compile(r"https?://[^\s<>)\"']+")


@dataclass
class Doc:
    name: str
    pages: list[str]          # pages[0] is page/slide 1; local text, also used by the injection scan
    pdf: bytes | None = None  # the same file as PDF (first MAX_PAGES pages), sent to Claude so it sees charts

    def as_text(self) -> str:
        return "\n".join(f"[{self.name} p.{i + 1}]\n{t}" for i, t in enumerate(self.pages))


@dataclass
class ReadResult:
    docs: list[Doc] = field(default_factory=list)
    not_read: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def anything_read(self) -> bool:
        return any(any(p.strip() for p in d.pages) for d in self.docs)


def extract(name: str, data: bytes) -> list[str]:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            raise ValueError("password protected")
        return [(p.extract_text() or "") for p in r.pages[:MAX_PAGES]]
    if ext == ".pptx":
        from pptx import Presentation
        pages = []
        for slide in list(Presentation(io.BytesIO(data)).slides)[:MAX_PAGES]:
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        parts.append(" | ".join(c.text for c in row.cells))
            pages.append("\n".join(parts))
        return pages
    if ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        text = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                text.append(" | ".join(c.text for c in row.cells))
        joined = "\n".join(text)
        # Word has no pages in the file; use ~3000-char chunks as "pages".
        return [joined[i:i + 3000] for i in range(0, max(len(joined), 1), 3000)][:MAX_PAGES]
    raise ValueError(f"format {ext or 'unknown'} is not read in version 1")


MAX_PDF_BYTES = 20 * 1024 * 1024


def trim_pdf(data: bytes) -> bytes | None:
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(io.BytesIO(data))
    if len(r.pages) > MAX_PAGES:
        w = PdfWriter()
        for p in r.pages[:MAX_PAGES]:
            w.add_page(p)
        b = io.BytesIO()
        w.write(b)
        data = b.getvalue()
    return data if len(data) <= MAX_PDF_BYTES else None


def to_pdf(name: str, data: bytes) -> bytes | None:
    """PowerPoint and Word are converted to PDF with LibreOffice when it is installed.
    Without it, the agent still has the extracted text."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return trim_pdf(data)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in{ext}"
        src.write_bytes(data)
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", d, str(src)],
                           check=True, capture_output=True, timeout=120)
            return trim_pdf((Path(d) / "in.pdf").read_bytes())
        except (subprocess.SubprocessError, OSError):
            return None


def fetch_google_link(kind: str, doc_id: str, client: httpx.Client) -> tuple[str, bytes]:
    url = f"https://docs.google.com/{kind}/d/{doc_id}/export?format=pdf"
    r = client.get(url, follow_redirects=True, timeout=30)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        raise ValueError("link is not open to anyone with the link")
    return f"google-{kind}-{doc_id}.pdf", r.content


def _read_file(res: ReadResult, name: str, data: bytes, convert: bool):
    try:
        res.docs.append(Doc(name, extract(name, data), to_pdf(name, data) if convert else None))
    except Exception as e:  # noqa: BLE001 - any failure is recorded, never silent
        res.not_read.append(f"{name} ({e})")


def read_email(email: Email, http: httpx.Client | None = None, earlier: list[Attachment] = (),
               convert: bool = True) -> ReadResult:
    """`earlier`: files kept from previous emails on the same deal, read again when a founder replies."""
    res = ReadResult()
    files = list(email.attachments)
    for extra in files[MAX_FILES:]:
        res.not_read.append(f"{extra.name} (over the {MAX_FILES}-file limit)")
    for a in list(earlier)[:MAX_FILES] + files[:MAX_FILES]:
        if Path(a.name).suffix.lower() not in READABLE_EXT:
            res.not_read.append(f"{a.name} (format not read in version 1)")
            continue
        _read_file(res, a.name, a.data, convert)

    urls = list(dict.fromkeys(URL.findall(email.body)))
    google = [u for u in urls if GOOGLE_LINK.match(u)]
    res.links = [u for u in urls if u not in google]
    for u in google:
        kind, doc_id = GOOGLE_LINK.match(u).groups()
        if len(res.docs) >= MAX_FILES + len(earlier):
            res.not_read.append(f"{u} (over the {MAX_FILES}-file limit)")
            continue
        try:
            if http is None:
                with httpx.Client() as c:
                    name, data = fetch_google_link(kind, doc_id, c)
            else:
                name, data = fetch_google_link(kind, doc_id, http)
        except Exception as e:  # noqa: BLE001
            res.not_read.append(f"{u} ({e})")
            continue
        _read_file(res, name, data, convert)
    for u in res.links:
        if re.search(r"docsend\.com|notion\.so|dropbox\.com|drive\.google\.com|\.key\b", u):
            res.not_read.append(f"{u} (link type not read in version 1)")
    return res
