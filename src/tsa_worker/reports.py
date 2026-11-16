"""TSA FOIA Reading Room weekly throughput reports: index, download, parse, Parquet cache.

The reading room (https://www.tsa.gov/foia/readingroom, category "Throughput
Data") lists one PDF per week, newest first, 20 per page. Titles are typed by
hand and vary ("TSA Throughput Data to November 1, 2026 to November 7, 2026",
"TSA Throughput February 12-18, 2017", stray commas and periods), so dates are
parsed leniently from the title, then from the file name.

* The index is cached as JSON for ``INDEX_TTL_SECONDS`` and refreshed
  incrementally: new reports appear on page 0, so a refresh stops at the first
  page that adds nothing new.
* A report PDF (~5 MB, ~1,400 pages) is downloaded once, parsed by
  `pdf_parse.parse_pdf` (~15 s) and cached as Parquet with a JSON sidecar
  holding row and reject counts; the PDF itself is then deleted. Rejected rows
  are kept in ``<key>.rejects.json``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .common import (
    POLITE_DELAY_SECONDS,
    Options,
    download,
    fetch_text,
    file_lock,
    public_url,
    read_json,
    write_json,
)
from .pdf_parse import parse_pdf

log = logging.getLogger(__name__)

THROUGHPUT_CATEGORY = "1132"
INDEX_TTL_SECONDS = 6 * 3600
# Bump when pdf_parse changes output, so cached Parquet is re-derived.
PARSER_VERSION = 1

_ROW_FIELDS: list[pa.Field[Any]] = [
    pa.field("date", pa.date32(), nullable=False),
    pa.field("hour", pa.int32(), nullable=False),
    pa.field("airport", pa.string(), nullable=False),
    pa.field("airport_name", pa.string()),
    pa.field("city", pa.string()),
    pa.field("state", pa.string(), nullable=False),
    pa.field("checkpoint", pa.string()),
    pa.field("throughput", pa.int64(), nullable=False),
]
ROW_SCHEMA = pa.schema(_ROW_FIELDS)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
}
_M = "(" + "|".join(_MONTHS) + ")"
_RANGE = re.compile(
    rf"{_M}\s*(\d{{1,2}})\s*((?:19|20)\d\d)?\s*(?:to|through|-|\u2013)\s*(?:{_M}\s*)?(\d{{1,2}})\s*((?:19|20)\d\d)",
    re.I,
)
_LINK = re.compile(r'<a\s+href="([^"]*/foia-readingroom/[^"]+)"[^>]*>(.*?)</a>(.*?)</(?:p|td)>', re.S | re.I)
_SIZE = re.compile(r"\(([\d.]+)\s*(KB|MB|GB)\)", re.I)
_PAGE = re.compile(r"[?&](?:amp;)?page=(\d+)")


def index_page_path(page: int) -> str:
    """tsa.gov-relative path of one page of the throughput reading-room index."""
    return f"foia/readingroom?field_foia_tax_category_target_id={THROUGHPUT_CATEGORY}&page={page}"


def parse_title_dates(*texts: str) -> tuple[dt.date, dt.date] | None:
    """Return the ``(first, last)`` day a report covers: from the title, else the file name (``texts[1:]``)."""
    for i, text in enumerate(texts):
        # Titles keep "12-18" style ranges; file names use hyphens and underscores as spaces.
        norm = re.sub(r"[.,_]+" if i == 0 else r"[.,_-]+", " ", html.unescape(text))
        norm = re.sub(r"\s+", " ", norm)
        m = _RANGE.search(norm)
        if not m:
            continue
        m1, d1, y1, m2, d2, y2 = m.groups()
        end_year = int(y2)
        try:
            end = dt.date(end_year, _MONTHS[(m2 or m1).lower()], int(d2))
            start = dt.date(int(y1) if y1 else end_year, _MONTHS[m1.lower()], int(d1))
            if start > end and not y1:
                start = start.replace(year=end_year - 1)
        except ValueError:
            continue
        if 0 <= (end - start).days <= 35:  # a few early reports cover two or three weeks
            return start, end
    return None


@dataclass(frozen=True, slots=True)
class Report:
    """One weekly report listed in the reading room."""

    path: str  # tsa.gov-relative path of the PDF
    title: str
    start_date: dt.date | None
    end_date: dt.date | None
    listed_size_mb: float | None

    @property
    def url(self) -> str:
        """Canonical tsa.gov URL."""
        return public_url(self.path)

    @property
    def key(self) -> str:
        """Cache file stem: the PDF's file name plus a short hash of its path."""
        stem = re.sub(r"[^a-z0-9-]+", "-", Path(self.path).stem.lower()).strip("-")[:80]
        return f"{stem}-{hashlib.sha256(self.path.encode()).hexdigest()[:8]}"


def parse_index_page(page: str) -> tuple[list[Report], int]:
    """Throughput reports listed on one index page, and the last page number in its pager."""
    reports: list[Report] = []
    for href, title_html, rest in _LINK.findall(page):
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title_html))).strip(" :")
        path = urllib.parse.urlsplit(html.unescape(href)).path.lstrip("/")
        if "throughput" not in title.lower() and "throughput" not in path.lower():
            continue
        if not path.lower().endswith(".pdf"):
            continue
        dates = parse_title_dates(title, Path(path).stem)
        size = _SIZE.search(re.sub(r"<[^>]+>", "", rest))
        mb: float | None = None
        if size:
            mb = float(size.group(1)) * {"kb": 1 / 1024, "mb": 1.0, "gb": 1024.0}[size.group(2).lower()]
        reports.append(
            Report(
                path=path,
                title=title,
                start_date=dates[0] if dates else None,
                end_date=dates[1] if dates else None,
                listed_size_mb=round(mb, 2) if mb is not None else None,
            )
        )
    pages = [int(p) for p in _PAGE.findall(page)]
    return reports, max(pages, default=0)


def _report_to_json(r: Report, order: int) -> dict[str, Any]:
    return {
        "path": r.path,
        "title": r.title,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "listed_size_mb": r.listed_size_mb,
        "order": order,
    }


def _report_from_json(d: dict[str, Any]) -> Report:
    return Report(
        path=d["path"],
        title=d["title"],
        start_date=dt.date.fromisoformat(d["start_date"]) if d.get("start_date") else None,
        end_date=dt.date.fromisoformat(d["end_date"]) if d.get("end_date") else None,
        listed_size_mb=d.get("listed_size_mb"),
    )


def _index_file(opts: Options) -> Path:
    return opts.source_root / "readingroom" / "throughput-index.json"


def _covers(data: Any, back_to: dt.date | None) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("complete"):
        return True
    if back_to is None:
        return False
    starts = [e["start_date"] for e in data.get("entries", []) if e.get("start_date")]
    return bool(starts) and min(starts) <= back_to.isoformat()


def load_index(opts: Options, back_to: dt.date | None = None) -> list[Report]:
    """Weekly reports, newest first. ``back_to`` = stop paging once reports reach that date."""
    path = _index_file(opts)
    cached = read_json(path)
    fresh = isinstance(cached, dict) and time.time() - float(cached.get("fetched_at", 0)) < INDEX_TTL_SECONDS
    if (fresh or opts.offline) and _covers(cached, back_to):
        assert isinstance(cached, dict)
        return [_report_from_json(e) for e in sorted(cached["entries"], key=lambda e: e["order"])]
    with file_lock(path.with_suffix(".lock")):
        cached = read_json(path)
        fresh = isinstance(cached, dict) and time.time() - float(cached.get("fetched_at", 0)) < INDEX_TTL_SECONDS
        if fresh and _covers(cached, back_to):
            assert isinstance(cached, dict)
            return [_report_from_json(e) for e in sorted(cached["entries"], key=lambda e: e["order"])]
        known: dict[str, Report] = {}
        was_complete = False
        if isinstance(cached, dict):
            known = {e["path"]: _report_from_json(e) for e in cached.get("entries", [])}
            was_complete = bool(cached.get("complete"))
        fetched: list[Report] = []
        complete = False
        page_no, last = 0, 0
        while True:
            if page_no and opts.is_remote:
                time.sleep(POLITE_DELAY_SECONDS)
            reports, last_here = parse_index_page(fetch_text(opts, index_page_path(page_no)))
            last = max(last, last_here)
            new = [r for r in reports if r.path not in known]
            fetched.extend(reports)
            if page_no >= last or not reports:
                complete = True
                break
            if was_complete and not new:
                complete = True  # everything older is already cached
                break
            dated = [r.end_date for r in reports if r.end_date]
            if back_to is not None and dated and max(dated) < back_to:
                break
            page_no += 1
        order: list[Report] = []
        seen: set[str] = set()
        for r in fetched:
            if r.path not in seen:
                seen.add(r.path)
                order.append(r)
        if isinstance(cached, dict):
            for e in sorted(cached.get("entries", []), key=lambda e: e["order"]):
                if e["path"] not in seen:
                    seen.add(e["path"])
                    order.append(_report_from_json(e))
        write_json(
            path,
            {
                "fetched_at": time.time(),
                "complete": complete or (was_complete and _covers(cached, back_to)),
                "entries": [_report_to_json(r, i) for i, r in enumerate(order)],
                "source_url": public_url(index_page_path(0)),
            },
        )
        return order


def select_reports(reports: list[Report], start: dt.date, end: dt.date) -> list[tuple[Report, dt.date, dt.date]]:
    """Which reports to read for ``[start, end]``, oldest first, with the dates each one supplies.

    A day is taken from the shortest listed report covering it (the latest posting
    on ties), so overlapping reports, such as a three-week file next to weekly
    ones, never double-count. Returns ``(report, first_day, last_day)`` triples.
    """
    owner: dict[dt.date, Report] = {}
    for r in reports:  # newest posting first
        if not (r.start_date and r.end_date and r.start_date <= end and r.end_date >= start):
            continue
        day = max(r.start_date, start)
        while day <= min(r.end_date, end):
            cur = owner.get(day)
            if cur is None or (r.end_date - r.start_date) < (cur.end_date - cur.start_date):  # type: ignore[operator]
                owner[day] = r
            day += dt.timedelta(days=1)
    spans: dict[str, tuple[Report, dt.date, dt.date]] = {}
    for day in sorted(owner):
        r = owner[day]
        first = spans[r.path][1] if r.path in spans else day
        spans[r.path] = (r, first, day)
    return sorted(spans.values(), key=lambda t: t[1])


# ------------------------------------------------------------------ parsed cache


def _parsed_dir(opts: Options) -> Path:
    return opts.source_root / "throughput"


def parsed_meta(opts: Options, report: Report) -> dict[str, Any] | None:
    """Sidecar of a parsed report, or None when it is not cached (or was parsed by an older parser)."""
    meta = read_json(_parsed_dir(opts) / f"{report.key}.json")
    if not isinstance(meta, dict) or meta.get("parser_version") != PARSER_VERSION:
        return None
    if not (_parsed_dir(opts) / f"{report.key}.parquet").exists():
        return None
    return meta


def cached_reports(opts: Options) -> list[dict[str, Any]]:
    """Sidecars of every parsed report in the cache."""
    out = []
    for p in sorted(_parsed_dir(opts).glob("*.json")) if _parsed_dir(opts).exists() else []:
        if p.name.endswith(".rejects.json"):
            continue
        meta = read_json(p)
        if isinstance(meta, dict):
            meta["path"] = str(p.with_suffix(".parquet"))
            out.append(meta)
    return out


def ensure_parsed(opts: Options, report: Report) -> tuple[pa.Table, list[dict[str, Any]], dict[str, Any]]:
    """Return ``(rows, rejects, meta)`` for a report, downloading and parsing it on first use."""
    d = _parsed_dir(opts)
    parquet, rejects_file = d / f"{report.key}.parquet", d / f"{report.key}.rejects.json"
    meta = parsed_meta(opts, report)
    if meta is None:
        with file_lock(d / f"{report.key}.lock"):
            meta = parsed_meta(opts, report)
            if meta is None:
                pdf = d / f"{report.key}.pdf"
                sha = download(opts, report.path, pdf)
                started = time.monotonic()
                try:
                    result = parse_pdf(pdf)
                finally:
                    pdf.unlink(missing_ok=True)
                rows = result.rows
                table = pa.table(
                    {
                        "date": pa.array([r.date for r in rows], pa.date32()),
                        "hour": pa.array([r.hour for r in rows], pa.int32()),
                        "airport": pa.array([r.airport for r in rows], pa.string()),
                        "airport_name": pa.array([r.airport_name for r in rows], pa.string()),
                        "city": pa.array([r.city for r in rows], pa.string()),
                        "state": pa.array([r.state for r in rows], pa.string()),
                        "checkpoint": pa.array([r.checkpoint for r in rows], pa.string()),
                        "throughput": pa.array([r.throughput for r in rows], pa.int64()),
                    },
                    schema=ROW_SCHEMA,
                )
                tmp = parquet.with_name(f"{parquet.name}.tmp")
                pq.write_table(table, tmp, compression="zstd")
                tmp.replace(parquet)
                write_json(rejects_file, [{"page": r.page, "reason": r.reason, "raw": r.raw} for r in result.rejects])
                if result.rejects:
                    log.warning("%s: %d rows failed validation (kept as rejects)", report.url, len(result.rejects))
                meta = {
                    "parser_version": PARSER_VERSION,
                    "source_url": report.url,
                    "title": report.title,
                    "start_date": report.start_date.isoformat() if report.start_date else None,
                    "end_date": report.end_date.isoformat() if report.end_date else None,
                    "sha256": sha,
                    "pages": result.pages,
                    "pages_without_table": result.pages_without_table,
                    "rows": len(rows),
                    "rejected_rows": len(result.rejects),
                    "first_date": min(r.date for r in rows).isoformat() if rows else None,
                    "last_date": max(r.date for r in rows).isoformat() if rows else None,
                    "parse_seconds": round(time.monotonic() - started, 1),
                    "parsed_at": time.time(),
                }
                write_json(d / f"{report.key}.json", meta)
    table = pq.read_table(parquet, schema=ROW_SCHEMA)
    rejects = read_json(rejects_file)
    return table, rejects if isinstance(rejects, list) else [], meta
