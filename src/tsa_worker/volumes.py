"""National daily checkpoint volumes from https://www.tsa.gov/travel/passenger-volumes.

TSA publishes one HTML table per calendar year: the current year on
``/travel/passenger-volumes`` (updated on weekdays, typically one to three days
behind) and earlier years on ``/travel/passenger-volumes/<year>`` (2019 onward).
Each year is cached as JSON: the current year for ``CURRENT_TTL_SECONDS``,
finished years for ``PAST_TTL_SECONDS``.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import time
from pathlib import Path

from .common import Options, fetch_text, file_lock, public_url, read_json, write_json

INDEX_PATH = "travel/passenger-volumes"
FIRST_YEAR = 2019
CURRENT_TTL_SECONDS = 3 * 3600
PAST_TTL_SECONDS = 30 * 24 * 3600

_ROW = re.compile(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_COUNT = re.compile(r"^\d{1,3}(,?\d{3})*$")
_YEAR_LINK = re.compile(r"/travel/passenger-volumes/((?:19|20)\d\d)\b")


class PageFormatError(RuntimeError):
    """A passenger-volumes page did not contain the expected Date / Numbers table."""


def _cell(raw: str) -> str:
    return html.unescape(_TAG.sub("", raw)).replace("\xa0", " ").strip()


def parse_volumes(page: str) -> list[tuple[dt.date, int]]:
    """Parse the Date / Numbers table of one passenger-volumes page, sorted by date.

    Args:
        page: The page's HTML.

    Returns:
        ``(date, travelers)`` pairs.

    Raises:
        PageFormatError: No parseable rows, or a row that is not ``M/D/YYYY`` + an integer count.

    """
    start = page.lower().find("<table")
    if start < 0:
        raise PageFormatError("no <table> on the passenger-volumes page")
    end = page.lower().find("</table>", start)
    table = page[start : end if end > 0 else len(page)]
    out: dict[dt.date, int] = {}
    for a, b in _ROW.findall(table):
        date_text, count_text = _cell(a), _cell(b)
        m = _DATE.match(date_text)
        if not m or not _COUNT.match(count_text):
            raise PageFormatError(f"unexpected passenger-volumes row: {date_text!r}, {count_text!r}")
        out[dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))] = int(count_text.replace(",", ""))
    if not out:
        raise PageFormatError("the passenger-volumes table has no rows")
    return sorted(out.items())


def year_links(page: str) -> list[int]:
    """Years that have their own page, as linked from the current-year page."""
    return sorted({int(y) for y in _YEAR_LINK.findall(page)})


def _cache_file(opts: Options, key: str) -> Path:
    return opts.source_root / "passenger-volumes" / f"{key}.json"


def _fresh(meta: object, ttl: float) -> bool:
    return isinstance(meta, dict) and time.time() - float(meta.get("fetched_at", 0)) < ttl


def load_current(opts: Options) -> dict[str, object]:
    """Load the current-year page: its rows, its year and the archive years it links to (cached)."""
    path = _cache_file(opts, "current")
    cached = read_json(path)
    if _fresh(cached, CURRENT_TTL_SECONDS) or (opts.offline and isinstance(cached, dict)):
        assert isinstance(cached, dict)
        return cached
    with file_lock(path.with_suffix(".lock")):
        cached = read_json(path)
        if _fresh(cached, CURRENT_TTL_SECONDS):
            assert isinstance(cached, dict)
            return cached
        page = fetch_text(opts, INDEX_PATH)
        rows = parse_volumes(page)
        data: dict[str, object] = {
            "fetched_at": time.time(),
            "year": max(d for d, _ in rows).year,
            "years": [y for y in year_links(page) if y >= FIRST_YEAR],
            "rows": [[d.isoformat(), n] for d, n in rows],
            "source_url": public_url(INDEX_PATH),
        }
        write_json(path, data)
        return data


def load_year(opts: Options, year: int) -> dict[str, object]:
    """One archived year's page (cached for ``PAST_TTL_SECONDS``)."""
    path = _cache_file(opts, str(year))
    cached = read_json(path)
    if _fresh(cached, PAST_TTL_SECONDS) or (opts.offline and isinstance(cached, dict)):
        assert isinstance(cached, dict)
        return cached
    with file_lock(path.with_suffix(".lock")):
        cached = read_json(path)
        if _fresh(cached, PAST_TTL_SECONDS):
            assert isinstance(cached, dict)
            return cached
        rel = f"{INDEX_PATH}/{year}"
        rows = parse_volumes(fetch_text(opts, rel))
        data: dict[str, object] = {
            "fetched_at": time.time(),
            "year": year,
            "rows": [[d.isoformat(), n] for d, n in rows if d.year == year],
            "source_url": public_url(rel),
        }
        write_json(path, data)
        return data


def available_years(opts: Options) -> tuple[int, list[int]]:
    """Return ``(current_year, archive_years)``; fetches the current-year page if needed."""
    current = load_current(opts)
    year = int(str(current["year"]))
    years = current.get("years")
    archive = [int(y) for y in years if int(y) < year] if isinstance(years, list) else []
    return year, archive


def rows_for_years(opts: Options, wanted: set[int] | None) -> list[tuple[dt.date, int, str]]:
    """``(date, travelers, source_url)`` for the wanted years (None = all), oldest first."""
    year, archive = available_years(opts)
    out: list[tuple[dt.date, int, str]] = []
    for y in sorted([*archive, year]):
        if wanted is not None and y not in wanted:
            continue
        data = load_current(opts) if y == year else load_year(opts, y)
        rows = data["rows"]
        assert isinstance(rows, list)
        url = str(data["source_url"])
        out.extend((dt.date.fromisoformat(d), int(n), url) for d, n in rows)
    return out
