import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chunking import chunk_text, clean_pdf_pages, split_sections  # noqa: E402
from sync_employees import validate  # noqa: E402

HANDBOOK_PDF = Path(__file__).resolve().parents[2] / "data" / "handbook" / \
    "Employee-Handbook-for-Nonprofits-and-Small-Businesses.pdf"


def test_markdown_sections_and_list_items():
    text = "# Handbook\n\n## 4 PTO\n\n1. Submit a request\n\n### 4.2 Accrual\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    sections = split_sections(text)
    assert [s for s, _ in sections] == ["Handbook > 4 PTO", "Handbook > 4 PTO > 4.2 Accrual"]
    assert "1. Submit a request" in sections[0][1]


def test_outline_headings_from_pdf_text():
    text = "V.\nBENEFITS AND LEAVES OF ABSENCE\n\nIntro.\n\nB. Vacation Benefits\n\nPlan.\n\n2. Accrual\n\nRates.\n\nC. Holidays\n\nList."
    assert [s for s, _ in split_sections(text, outline_headings=True)] == [
        "V. BENEFITS AND LEAVES OF ABSENCE",
        "V. BENEFITS AND LEAVES OF ABSENCE > B. Vacation Benefits",
        "V. BENEFITS AND LEAVES OF ABSENCE > B. Vacation Benefits > 2. Accrual",
        "V. BENEFITS AND LEAVES OF ABSENCE > C. Holidays",
    ]


def test_clean_pdf_pages_drops_headers_page_numbers_and_toc_but_keeps_body_numbers():
    pages = [f"HEADER LINE\n{i}\nBody text {i}\nmore text {i}" for i in range(1, 6)]
    pages[0] += "\nB. Vacation Benefits ........ 13"
    pages[2] = pages[2].replace("Body text 3", "Body text 3\nDate of hire through end of year\n5\n3.077 hours biweekly")
    out = clean_pdf_pages(pages)
    assert "HEADER LINE" not in out and "........" not in out
    assert not out.startswith("1\n") and "\n2\n" not in out
    assert "end of year\n5\n3.077" in out


def test_chunks_respect_max_size():
    text = "# T\n\n" + "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(30))
    chunks = chunk_text(text, max_chars=800, overlap=100)
    assert len(chunks) > 1 and all(len(c.content) <= 800 + 2 for c in chunks)


@pytest.mark.skipif(not HANDBOOK_PDF.exists(), reason="handbook PDF not present")
def test_real_handbook_accrual_table_stays_in_one_chunk():
    pytest.importorskip("pypdf")
    from ingest_handbook import extract_text
    text = extract_text(HANDBOOK_PDF.name, HANDBOOK_PDF.read_bytes())
    accrual = [c for c in chunk_text(text, outline_headings=True) if c.section.endswith("2. Accrual")
               and "Vacation" in c.section]
    assert accrual
    body = accrual[0].content
    for needle in ("3.077 hours biweekly", "4.62 hours biweekly", "6.15 hours biweekly", "90 calendar days"):
        assert needle in body


def test_sync_validate_rejects_bad_rows_and_dedupes():
    rows = [
        {"employee_id": "E1", "first_name": "A", "last_name": "B", "email": "a@b.org", "start_date": "2020-01-01",
         "employment_type": "full_time", "updated_at": "2026-01-01T00:00:00Z"},
        {"employee_id": "E1", "first_name": "A", "last_name": "C", "email": "a@b.org", "start_date": "2020-01-01",
         "employment_type": "FULL_TIME", "updated_at": "2026-02-01T00:00:00Z"},
        {"employee_id": "bad id", "start_date": "2020-01-01", "employment_type": "FULL_TIME"},
        {"employee_id": "E2", "start_date": "not a date", "employment_type": "FULL_TIME"},
        {"employee_id": "E3", "start_date": "2020-01-01", "employment_type": "CONTRACTOR"},
    ]
    valid, skipped = validate(rows)
    assert skipped == 3
    assert len(valid) == 1 and valid[0]["full_name"] == "A C"
    assert valid[0]["start_date"] == date(2020, 1, 1) and valid[0]["employment_type"] == "FULL_TIME"
