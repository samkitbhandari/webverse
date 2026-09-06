"""Document parsing and semantic chunking.

Docling is used when it is installed, because layout-aware extraction keeps
tables and heading hierarchy intact and that structure is what makes chunking
meaningful. It is an optional dependency though -- it pulls torch and roughly
three gigabytes of model weights -- so every format also has a lightweight
path. The pipeline behaves identically either way; only extraction fidelity on
complex PDFs differs, and ``ParsedDocument.parser`` records which was used.

Chunking follows the document's own hierarchy rather than a fixed window. A
claim and the heading that gives it context ("## Thermal limits") must stay
together, or the extractor loses the subject the heading was carrying.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.config import settings
from backend.core.ids import fingerprint

log = logging.getLogger("nexus.parsing")

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".json", ".yaml", ".yml"}
SUPPORTED = TEXT_SUFFIXES | {".pdf", ".docx", ".doc", ".html", ".htm", ".pptx"}


@dataclass
class Chunk:
    index: int
    text: str
    locator: str = ""              # "Section > Subsection"
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.text)


@dataclass
class ParsedDocument:
    path: Path
    title: str
    markdown: str
    parser: str = "plain"
    chunks: list[Chunk] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def fingerprints(self) -> list[str]:
        return [c.fingerprint for c in self.chunks]


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
_docling_converter = None
_docling_checked = False


def _docling():
    """Lazily construct the Docling converter; None if unavailable."""
    global _docling_converter, _docling_checked
    if _docling_checked:
        return _docling_converter
    _docling_checked = True
    try:
        from docling.document_converter import DocumentConverter

        _docling_converter = DocumentConverter()
        log.info("docling available: layout-aware parsing enabled")
    except Exception as exc:
        log.info("docling unavailable (%s); using lightweight parsers", type(exc).__name__)
        _docling_converter = None
    return _docling_converter


def _parse_with_docling(path: Path) -> tuple[str, dict] | None:
    conv = _docling()
    if conv is None:
        return None
    try:
        result = conv.convert(str(path))
        md = result.document.export_to_markdown()
        meta = {"pages": getattr(result.document, "num_pages", None)}
        return md, meta
    except Exception as exc:
        log.warning("docling failed on %s (%s); falling back", path.name, exc)
        return None


def _parse_pdf(path: Path) -> str:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        log.warning("pypdfium2 not installed; cannot read %s", path.name)
        return ""
    out: list[str] = []
    try:
        doc = pdfium.PdfDocument(str(path))
        for i in range(len(doc)):
            page = doc[i]
            text = page.get_textpage().get_text_range()
            if text.strip():
                out.append(f"\n## Page {i + 1}\n\n{text.strip()}")
        doc.close()
    except Exception as exc:
        log.warning("pdf extraction failed for %s: %s", path.name, exc)
    return "\n".join(out)


def _parse_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        log.warning("python-docx not installed; cannot read %s", path.name)
        return ""
    try:
        d = docx.Document(str(path))
    except Exception as exc:
        log.warning("docx extraction failed for %s: %s", path.name, exc)
        return ""

    lines: list[str] = []
    for p in d.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "").lower() if p.style else ""
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            lines.append(f"\n{'#' * min(int(level), 6)} {text}\n")
        else:
            lines.append(text)
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _parse_html(path: Path) -> str:
    raw = path.read_text("utf-8", errors="replace")
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>",
                 lambda m: f"\n{'#' * int(m.group(1))} {m.group(2)}\n", raw, flags=re.S | re.I)
    raw = re.sub(r"<(p|div|li|tr|br)[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    import html as _html

    return re.sub(r"\n{3,}", "\n\n", _html.unescape(raw))


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def chunk_markdown(
    md: str, max_chars: int | None = None, min_chars: int | None = None
) -> list[Chunk]:
    """Split on heading hierarchy, keeping each chunk under a size ceiling.

    Oversized sections are split on paragraph boundaries and the heading path is
    repeated on every piece, so a fragment never arrives at the extractor
    without the context that says what it is about.
    """
    max_chars = max_chars or settings.max_chunk_chars
    min_chars = min_chars or settings.min_chunk_chars

    stack: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = []
    locator = ""

    for line in (md or "").splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            if current:
                sections.append((locator, current))
                current = []
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = stack[: level - 1] + [title]
            locator = " > ".join(stack)
        else:
            current.append(line)
    if current:
        sections.append((locator, current))

    chunks: list[Chunk] = []
    carry = ""
    carry_locator = ""

    def emit(text: str, loc: str) -> None:
        t = text.strip()
        if t:
            chunks.append(Chunk(index=len(chunks), text=t, locator=loc))

    for loc, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        # Marked as a heading so downstream extraction can tell context from
        # content; a bare breadcrumb line reads as a sentence full of proper
        # nouns and hijacks subject selection.
        header = f"## {loc}\n\n" if loc else ""

        # merge sections too small to stand alone
        if len(body) < min_chars:
            carry = f"{carry}\n\n{header}{body}" if carry else f"{header}{body}"
            carry_locator = carry_locator or loc
            if len(carry) >= min_chars:
                emit(carry, carry_locator)
                carry, carry_locator = "", ""
            continue

        if carry:
            emit(carry, carry_locator)
            carry, carry_locator = "", ""

        if len(body) <= max_chars:
            emit(f"{header}{body}", loc)
            continue

        buf: list[str] = []
        size = 0
        for para in re.split(r"\n\s*\n", body):
            p = para.strip()
            if not p:
                continue
            if size + len(p) > max_chars and buf:
                emit(header + "\n\n".join(buf), loc)
                buf, size = [], 0
            buf.append(p)
            size += len(p) + 2
        if buf:
            emit(header + "\n\n".join(buf), loc)

    if carry:
        emit(carry, carry_locator)
    return chunks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse(path: Path | str) -> ParsedDocument:
    """Parse a source file into markdown plus semantic chunks."""
    path = Path(path)
    suffix = path.suffix.lower()
    title = path.stem.replace("_", " ").replace("-", " ").strip().title()
    parser = "plain"
    metadata: dict = {}
    md = ""

    if not path.exists():
        log.warning("source vanished before parsing: %s", path)
        return ParsedDocument(path=path, title=title, markdown="", parser="missing")

    if suffix in (".pdf", ".docx", ".doc", ".pptx", ".html", ".htm"):
        if result := _parse_with_docling(path):
            md, metadata = result
            parser = "docling"

    if not md:
        if suffix == ".pdf":
            md, parser = _parse_pdf(path), "pypdfium2"
        elif suffix in (".docx", ".doc"):
            md, parser = _parse_docx(path), "python-docx"
        elif suffix in (".html", ".htm"):
            md, parser = _parse_html(path), "html-strip"
        elif suffix in TEXT_SUFFIXES or not suffix:
            md, parser = path.read_text("utf-8", errors="replace"), "plain"
        else:
            log.info("unsupported source type %s: %s", suffix, path.name)
            return ParsedDocument(path=path, title=title, markdown="", parser="unsupported")

    # a leading H1 is a better title than the filename
    if m := re.search(r"^#\s+(.+)$", md, re.M):
        title = m.group(1).strip()[:120]

    doc = ParsedDocument(path=path, title=title, markdown=md, parser=parser,
                         metadata=metadata)
    doc.chunks = chunk_markdown(md)
    log.info("parsed %s via %s: %d chars, %d chunk(s)",
             path.name, parser, len(md), len(doc.chunks))
    return doc


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED
