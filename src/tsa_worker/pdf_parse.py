"""Parse TSA FOIA "throughput" PDFs into hourly checkpoint rows.

TSA exports these reports from MicroStrategy as one long table drawn on ~1,400
pages: Date, Hour of Day, Airport (code + name), City, State, Checkpoint and a
passenger count. Plain text extraction jumbles the columns (date and hour cells
are vertically merged and centred, long airport codes wrap), so this parser reads
the drawn table instead:

1. Every table cell is a drawn rectangle. The header labels ("Date", "Hour",
   "Airport", ...) pick out which column of cells holds which field.
2. Each text run is assigned to the smallest body cell containing its centre,
   and a cell's text is its runs joined in reading order.
3. Each cell of the count column is one output row; the other columns take the
   cell whose vertical span contains that row (merged cells cover many rows).
   An empty cell that starts at the top of the table continues the previous
   page's value.

Every row is validated (date parses, hour 0-23, 3-letter airport code, 2-letter
state, integer count). Rows that fail are kept as `Reject` records, never dropped
silently. Both layouts TSA has used are handled: the 2017-2022 "PMIS - Total
Customer Throughput (Unadjusted)" export and the 2023+ "Total Pax + Crew PAX" one.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

COLUMNS = ("date", "hour", "airport", "airport_name", "city", "state", "checkpoint", "throughput")

_LABELS = {
    "date": re.compile(r"^date$", re.I),
    "hour": re.compile(r"^hour( of)?$", re.I),
    "airport": re.compile(r"^airport$", re.I),
    "city": re.compile(r"^city$", re.I),
    "state": re.compile(r"^state$", re.I),
    "checkpoint": re.compile(r"^checkpoint$", re.I),
    "throughput": re.compile(r"^(total pax|pmis - total)", re.I),
}
# Narrow cells wrap mid-token ("12/10/202" + "1", "MD" + "W"): join their runs without spaces.
_UNSPACED = frozenset({"date", "hour", "airport"})
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_HOUR = re.compile(r"^(\d{1,2}):00$")
_IATA = re.compile(r"^[A-Z0-9]{3}$")
_STATE = re.compile(r"^[A-Z]{2}$")
_COUNT = re.compile(r"^\d{1,3}(,?\d{3})*$")


@dataclass(slots=True)
class _Cell:
    x0: float
    y0: float
    x1: float
    y1: float
    texts: list[tuple[float, float, str]] = field(default_factory=list)

    def contains(self, x: float, y: float) -> bool:
        return self.x0 - 0.5 <= x <= self.x1 + 0.5 and self.y0 - 0.5 <= y <= self.y1 + 0.5

    def text(self, sep: str = " ") -> str:
        parts = [t for _, _, t in sorted(self.texts, key=lambda r: (-r[0], r[1]))]
        return re.sub(r"\s+", " ", sep.join(p.strip() for p in parts)).strip()


@dataclass(frozen=True, slots=True)
class Row:
    """One validated hourly row."""

    date: dt.date
    hour: int
    airport: str
    airport_name: str | None
    city: str | None
    state: str
    checkpoint: str | None
    throughput: int


@dataclass(frozen=True, slots=True)
class Reject:
    """A table row that failed validation, with the raw cell text."""

    page: int
    reason: str
    raw: dict[str, str | None]


@dataclass(slots=True)
class ParseResult:
    """Everything one PDF yielded."""

    rows: list[Row]
    rejects: list[Reject]
    pages: int
    pages_without_table: int


def _page_cells(page: Any) -> list[_Cell]:
    cells: dict[tuple[int, int, int, int], _Cell] = {}
    for obj in page.get_objects(max_depth=4):
        if obj.type != pdfium_raw.FPDF_PAGEOBJ_PATH:
            continue
        x0, y0, x1, y1 = obj.get_bounds()
        w, h = x1 - x0, y1 - y0
        if w <= 5 or h <= 5 or w >= 300 or h >= 1000:
            continue
        key = (round(x0), round(y0), round(x1), round(y1))
        cells.setdefault(key, _Cell(x0, y0, x1, y1))
    return list(cells.values())


def _page_texts(page: Any) -> list[tuple[float, float, float, float, str]]:
    tp = page.get_textpage()
    out = []
    for i in range(tp.count_rects()):
        x0, y0, x1, y1 = tp.get_rect(i)
        text = tp.get_text_bounded(x0, y0, x1, y1).strip()
        if text:
            out.append((x0, y0, x1, y1, text))
    return out


def _smallest(cells: list[_Cell], x: float, y: float) -> _Cell | None:
    best: _Cell | None = None
    for c in cells:
        if c.contains(x, y) and (
            best is None or (c.x1 - c.x0) * (c.y1 - c.y0) < (best.x1 - best.x0) * (best.y1 - best.y0)
        ):
            best = c
    return best


def _validate(raw: dict[str, str | None]) -> Row | str:
    """Return a `Row`, or the reason the raw cells are not one."""
    m = _DATE.match(raw["date"] or "")
    if not m:
        return f"bad date {raw['date']!r}"
    try:
        day = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return f"bad date {raw['date']!r}"
    h = _HOUR.match(raw["hour"] or "")
    if not h or not 0 <= int(h.group(1)) <= 23:
        return f"bad hour {raw['hour']!r}"
    code = (raw["airport"] or "").upper()
    if not _IATA.match(code):
        return f"bad airport code {raw['airport']!r}"
    state = (raw["state"] or "").upper()
    if not _STATE.match(state):
        return f"bad state {raw['state']!r}"
    count = raw["throughput"] or ""
    if not _COUNT.match(count):
        return f"bad count {raw['throughput']!r}"
    return Row(
        date=day,
        hour=int(h.group(1)),
        airport=code,
        airport_name=raw["airport_name"] or None,
        city=(raw["city"] or "").rstrip(", ") or None,
        state=state,
        checkpoint=raw["checkpoint"] or None,
        throughput=int(count.replace(",", "")),
    )


def _label_centres(texts: list[tuple[float, float, float, float, str]]) -> tuple[dict[str, tuple[float, float]], float]:
    """Find each header label's centre, and the lowest label's bottom edge."""
    found: dict[str, tuple[float, float]] = {}
    lowest = 0.0
    for x0, y0, x1, y1, text in texts:
        for name, pat in _LABELS.items():
            if name not in found and pat.match(text.strip()):
                found[name] = ((x0 + x1) / 2, (y0 + y1) / 2)
                lowest = y0 if lowest == 0.0 else min(lowest, y0)
    return found, lowest


def _body_columns(labels: dict[str, tuple[float, float]], body: list[_Cell]) -> dict[str, list[_Cell]] | None:
    """Group body cells into the output columns, using the labels' x positions."""
    groups: dict[tuple[int, int], list[_Cell]] = {}
    for c in body:
        groups.setdefault((round(c.x0), round(c.x1)), []).append(c)
    spans = sorted(groups)
    cols: dict[str, list[_Cell]] = {}
    for name, (lx, _) in labels.items():
        under = [s for s in spans if s[0] - 1 <= lx <= s[1] + 1]
        if not under:
            return None
        best = max(under, key=lambda s: len(groups[s]))
        cols[name] = groups[best]
        if name == "airport":
            # Below the Airport header sit two columns: the code, then the name.
            right = [s for s in spans if abs(s[0] - best[1]) <= 3]
            if not right:
                return None
            cols["airport_name"] = groups[max(right, key=lambda s: len(groups[s]))]
    return cols


def parse_page(page: Any, page_no: int, carry: dict[str, str | None]) -> tuple[list[Row], list[Reject]] | None:
    """Parse one page; ``carry`` holds the previous page's last value per column. None = no table."""
    texts = _page_texts(page)
    labels, label_bottom = _label_centres(texts)
    if len(labels) != len(_LABELS):
        return None
    body = [c for c in _page_cells(page) if c.y1 <= label_bottom + 0.5]
    cols = _body_columns(labels, body)
    if cols is None or not body:
        return None
    bottom = max(c.y1 for c in body)
    every = [c for group in cols.values() for c in group]
    for x0, y0, x1, y1, text in texts:
        cy = (y0 + y1) / 2
        if cy >= bottom:
            continue
        cell = _smallest(every, (x0 + x1) / 2, cy)
        if cell is not None:
            cell.texts.append((y1, x0, text))
    rows: list[Row] = []
    rejects: list[Reject] = []
    for leaf in sorted(cols["throughput"], key=lambda c: -c.y1):
        count = leaf.text("")
        if not count:
            continue  # trailing empty grid cell
        y = (leaf.y0 + leaf.y1) / 2
        raw: dict[str, str | None] = {}
        for name in COLUMNS:
            if name == "throughput":
                raw[name] = count
                continue
            cell = next((c for c in cols[name] if c.y0 - 0.5 <= y <= c.y1 + 0.5), None)
            value = cell.text("" if name in _UNSPACED else " ") if cell is not None else ""
            if not value and cell is not None and cell.y1 >= bottom - 3:
                value = carry.get(name) or ""  # merged cell continued from the previous page
            raw[name] = value or None
            if value:
                carry[name] = value
        result = _validate(raw)
        if isinstance(result, Row):
            rows.append(result)
        else:
            rejects.append(Reject(page=page_no, reason=result, raw=raw))
    return rows, rejects


def iter_pages(path: Path) -> Iterator[tuple[int, Any]]:
    """Yield ``(page_number, page)`` (1-based) from a PDF, closing it afterwards."""
    doc = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(doc)):
            page = doc[i]
            try:
                yield i + 1, page
            finally:
                page.close()
    finally:
        doc.close()


def parse_pdf(path: Path) -> ParseResult:
    """Parse a whole throughput PDF."""
    rows: list[Row] = []
    rejects: list[Reject] = []
    pages = 0
    empty = 0
    carry: dict[str, str | None] = {}
    for page_no, page in iter_pages(path):
        pages += 1
        got = parse_page(page, page_no, carry)
        if got is None:
            empty += 1
            continue
        rows.extend(got[0])
        rejects.extend(got[1])
    return ParseResult(rows=rows, rejects=rejects, pages=pages, pages_without_table=empty)
