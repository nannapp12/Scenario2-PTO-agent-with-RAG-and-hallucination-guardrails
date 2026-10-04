"""Section-aware chunking for the Employee Handbook.

PDF text is cleaned first: running page headers/footers, bare page numbers and
table-of-contents lines (dot leaders) are removed. Sections are then split on
headings — Markdown "#" headings, or the handbook's outline headings in PDF text
("V. BENEFITS AND LEAVES OF ABSENCE", "B. Vacation Benefits", "2. Accrual") — and
whole paragraphs are packed into chunks of at most `max_chars`. Each chunk keeps its
heading path (e.g. "V. BENEFITS AND LEAVES OF ABSENCE > B. Vacation Benefits > 2. Accrual")
for citations.
"""
import re
from collections import Counter
from dataclasses import dataclass

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Outline headings in PDF text: level 1 roman, level 2 letter, level 3 number.
_ROMAN = re.compile(r"^([IVX]{1,5})\.\s*(.*)$")
_LETTER = re.compile(r"^([A-Z])\.\s+([A-Z][^.!?:;]{0,78})$")
_NUMBER = re.compile(r"^(\d{1,2})\.\s+([A-Z][^.!?:;]{0,78})$")
_TOC = re.compile(r"(\.{4,}|…{2,})")
_PAGE_NO = re.compile(r"^\d{1,3}$")


@dataclass(frozen=True)
class Chunk:
    section: str
    content: str


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def clean_pdf_pages(pages: list[str]) -> str:
    """Join PDF pages, dropping lines repeated on most pages (running headers/footers),
    bare page numbers and table-of-contents lines."""
    pages = [normalize(p) for p in pages]
    counts = Counter(line.strip() for p in pages for line in set(p.split("\n")) if line.strip())
    repeated = {line for line, n in counts.items() if len(pages) >= 4 and n >= len(pages) / 2}
    kept = []
    for p in pages:
        lines = [ln.rstrip() for ln in p.split("\n") if ln.strip() not in repeated and not _TOC.search(ln)]
        content = [i for i, ln in enumerate(lines) if ln.strip()]
        edges = set(content[:3] + content[-2:])  # page numbers sit at the top or bottom only
        lines = [ln for i, ln in enumerate(lines) if not (i in edges and _PAGE_NO.match(ln.strip()))]
        kept.append("\n".join(lines).strip())
    return "\n\n".join(kept)


def _outline_heading(line: str, next_line: str) -> tuple[int, str] | None:
    s = line.strip()
    if m := _ROMAN.match(s):
        title = m.group(2).strip() or next_line.strip()   # "V." alone, title on the next line
        if title and title.upper() == title:              # roman headings are ALL CAPS here
            return 1, f"{m.group(1)}. {title}"
    if m := _LETTER.match(s):
        return 2, s
    if m := _NUMBER.match(s):
        return 3, s
    return None


def split_sections(text: str, outline_headings: bool = False) -> list[tuple[str, str]]:
    """outline_headings: also detect the PDF outline headings (off for Markdown,
    where "1. Do this" is a list item)."""
    sections: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []
    body: list[str] = []

    def flush():
        content = "\n".join(body).strip()
        if content:
            sections.append((" > ".join(t for _, t in path), content))
        body.clear()

    lines = normalize(text).split("\n")
    skip: set[int] = set()
    for i, line in enumerate(lines):
        if i in skip:
            continue
        heading = None
        if m := _MD_HEADING.match(line):
            heading = (len(m.group(1)), m.group(2))
        elif outline_headings:
            j = next((k for k in range(i + 1, min(i + 3, len(lines))) if lines[k].strip()), None)
            heading = _outline_heading(line, lines[j] if j is not None else "")
            roman = _ROMAN.match(line.strip())
            if heading and roman and not roman.group(2).strip() and j is not None:
                skip.add(j)  # the title line was consumed into the heading
        if heading:
            flush()
            level, title = heading
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title.strip())]
        else:
            body.append(line)
    flush()
    return sections


def _split_long(paragraph: str, max_chars: int, overlap: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    parts, current = [], ""
    for s in sentences:
        while len(s) > max_chars:  # a single huge "sentence": hard split
            parts.append(s[:max_chars])
            s = s[max_chars - overlap:]
        if current and len(current) + 1 + len(s) > max_chars:
            parts.append(current)
            current = current[-overlap:].lstrip() + " " + s if overlap else s
        else:
            current = f"{current} {s}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_text(text: str, max_chars: int = 1500, overlap: int = 200,
               outline_headings: bool = False) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section, body in split_sections(text, outline_headings):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        current: list[str] = []
        size = 0
        for p in paragraphs:
            pieces = _split_long(p, max_chars, overlap) if len(p) > max_chars else [p]
            for piece in pieces:
                if current and size + 2 + len(piece) > max_chars:
                    chunks.append(Chunk(section, "\n\n".join(current)))
                    # carry the previous paragraph forward when it's short, for context
                    current = [current[-1]] if len(current[-1]) <= overlap else []
                    size = sum(len(c) for c in current)
                current.append(piece)
                size += len(piece) + 2
        if current:
            chunks.append(Chunk(section, "\n\n".join(current)))
    return chunks
