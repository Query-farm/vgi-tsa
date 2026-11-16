"""Build test/fixtures/tsa: a trimmed, offline mirror of the tsa.gov pages and PDFs the worker reads.

    uv run python scripts/make_fixtures.py RAW_DIR

RAW_DIR holds the files as downloaded from tsa.gov (fetch them politely, about one
request per second, with a generic User-Agent):

    pv-current.html            https://www.tsa.gov/travel/passenger-volumes
    pv-<year>.html             https://www.tsa.gov/travel/passenger-volumes/<year>   (2019..)
    rr-<n>.html                https://www.tsa.gov/foia/readingroom?field_foia_tax_category_target_id=1132&page=<n>
    <report>.pdf               the weekly PDFs named in FIXTURE_PDFS

The HTML is cut down to the data table (and, for the reading room, its pager),
keeping the original markup. Each PDF keeps only the pages in FIXTURE_PDFS, which
preserves TSA's drawn table exactly. Everything is a U.S. government work (public domain).
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test" / "fixtures" / "tsa"
PDF_DIR = "sites/default/files/foia-readingroom"

# report file -> 0-based pages to keep (first pages, a middle page and the last page).
FIXTURE_PDFS = {
    "tsa-throughput-data-to-november-1-2026-to-november-7-2026.pdf": [0, 1, 2, 700, 1364],
    "tsa-throughput-december-5-2021-to-december-11-2021.pdf": [0, 1, 841],
}


def _wrap(body: str) -> str:
    return f"<!DOCTYPE html>\n<html><body>\n{body}\n</body></html>\n"


def _volumes(raw: str) -> str:
    table = raw[raw.find("<table") : raw.find("</table>") + len("</table>")]
    years = sorted(set(re.findall(r'href="(/travel/passenger-volumes/\d{4})"', raw)))
    links = "\n".join(f'<li><a href="{y}">{y[-4:]}</a></li>' for y in years)
    return _wrap(f"<ul>\n{links}\n</ul>\n{table}")


def _readingroom(raw: str) -> str:
    start = raw.find('class="view-content')
    table = raw[raw.find("<table", start) : raw.find("</table>", start) + len("</table>")]
    p = raw.find('<ul class="usa-pagination__list')
    pager = raw[p : raw.find("</ul>", p) + len("</ul>")] if p >= 0 else ""
    return _wrap(f'<div class="view-content">\n{table}\n</div>\n{pager}')


def _write(rel: str, text: str) -> None:
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(raw_dir: Path) -> None:
    """Write the fixture mirror from the raw downloads in ``raw_dir``."""
    _write("travel/passenger-volumes/index.html", _volumes((raw_dir / "pv-current.html").read_text()))
    for f in sorted(raw_dir.glob("pv-[0-9]*.html")):
        year = f.stem.removeprefix("pv-")
        _write(f"travel/passenger-volumes/{year}/index.html", _volumes(f.read_text()))
    for f in sorted(raw_dir.glob("rr-*.html")):
        n = int(f.stem.removeprefix("rr-"))
        _write(f"foia/readingroom/page-{n}.html", _readingroom(f.read_text()))
    for name, pages in FIXTURE_PDFS.items():
        src = pdfium.PdfDocument(str(raw_dir / name))
        dst = pdfium.PdfDocument.new()
        dst.import_pages(src, pages)
        target = OUT / PDF_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        dst.save(str(target))
        print(name, target.stat().st_size, hashlib.sha256((raw_dir / name).read_bytes()).hexdigest(), file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
