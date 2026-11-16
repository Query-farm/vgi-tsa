"""Unit tests. The SQL-level tests live in test/sql and run through DuckDB."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments
from vgi.client import Client

from tsa_worker import reports, volumes
from tsa_worker.common import Options, mirror_path, options_from, read_json, write_json
from tsa_worker.pdf_parse import _validate, parse_pdf

D = dt.date
FIXTURES = Path(__file__).resolve().parent.parent / "test" / "fixtures" / "tsa"
PDF_DIR = FIXTURES / "sites" / "default" / "files" / "foia-readingroom"


# ----------------------------------------------------------------- common


def test_mirror_path_layout() -> None:
    assert mirror_path("travel/passenger-volumes") == "travel/passenger-volumes/index.html"
    assert mirror_path("travel/passenger-volumes/2024") == "travel/passenger-volumes/2024/index.html"
    assert mirror_path(reports.index_page_path(3)) == "foia/readingroom/page-3.html"
    assert mirror_path("/sites/default/files/foia-readingroom/x.pdf") == "sites/default/files/foia-readingroom/x.pdf"


def test_options_defaults_and_limits(tmp_path: Path) -> None:
    o = options_from({"cache_dir": str(tmp_path)})
    assert o.is_remote and o.max_reports == 8 and o.source_root.name == "tsa.gov"
    assert options_from({"cache_dir": str(tmp_path), "max_reports": pa.scalar(20)}).max_reports == 20
    with pytest.raises(ValueError, match="at least 1"):
        options_from({"max_reports": 0})


# ---------------------------------------------------------------- volumes


def test_parse_volumes_page() -> None:
    page = (FIXTURES / "travel/passenger-volumes/2024/index.html").read_text()
    rows = volumes.parse_volumes(page)
    assert len(rows) == 366  # leap year
    assert rows[0] == (D(2024, 1, 1), 2303507)
    assert volumes.year_links(page) == list(range(2019, 2026))


def test_parse_volumes_rejects_garbage() -> None:
    with pytest.raises(volumes.PageFormatError):
        volumes.parse_volumes("<table><tr><td>soon</td><td>n/a</td></tr></table>")
    with pytest.raises(volumes.PageFormatError):
        volumes.parse_volumes("<p>no table</p>")


def test_rows_for_years_fetches_only_wanted(opts: Options) -> None:
    rows = volumes.rows_for_years(opts, {2020})
    assert {r[0].year for r in rows} == {2020} and len(rows) == 366
    cached = sorted(p.stem for p in (opts.source_root / "passenger-volumes").glob("*.json"))
    assert cached == ["2020", "current"]


def test_offline_uses_stale_cache(opts: Options, tmp_path: Path) -> None:
    volumes.rows_for_years(opts, {2026})
    offline = options_from({"cache_dir": str(opts.cache_dir), "base_url": str(tmp_path / "nowhere"), "offline": True})
    # A different base_url is a different cache root, so offline has nothing to read.
    with pytest.raises(RuntimeError, match="offline"):
        volumes.rows_for_years(offline, {2026})


# ---------------------------------------------------------------- reports


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("TSA Throughput Data to November 1, 2026 to November 7, 2026", (D(2026, 11, 1), D(2026, 11, 7))),
        ("TSA Throughput February 19-25, 2017", (D(2017, 2, 19), D(2017, 2, 25))),
        ("TSA Throughput March 26-April 1, 2017", (D(2017, 3, 26), D(2017, 4, 1))),
        ("TSA Throughput Data to February 2, 2025 to February 8. 2025", (D(2025, 2, 2), D(2025, 2, 8))),
        ("TSA Throughput Data September 13, 2020  to September 19 , 2020", (D(2020, 9, 13), D(2020, 9, 19))),
        ("TSA Throughput Data to November 9, 2025, to November 15, 2025", (D(2025, 11, 9), D(2025, 11, 15))),
    ],
)
def test_parse_title_dates(title: str, expected: tuple[D, D]) -> None:
    assert reports.parse_title_dates(title) == expected


def test_parse_title_dates_falls_back_to_file_name() -> None:
    # TSA's typo: "August 30 ... to August 9" is not a range; the file name is right.
    got = reports.parse_title_dates(
        "TSA Throughput Data to August 30, 2025 to August 9, 2025",
        "tsa-throughput-data-to-august-3-2025-to-august-9-2025",
    )
    assert got == (D(2025, 8, 3), D(2025, 8, 9))
    assert reports.parse_title_dates("Firearms Discovered 2022") is None


def test_parse_index_page_skips_other_documents() -> None:
    page = (FIXTURES / "foia/readingroom/page-26.html").read_text()
    found, last = reports.parse_index_page(page)
    assert last == 26
    assert all("throughput" in r.title.lower() for r in found)
    assert all(r.start_date is not None for r in found)


def test_load_index_full_and_incremental(opts: Options) -> None:
    full = reports.load_index(opts)
    assert len(full) == 517 and full[0].start_date == D(2026, 11, 1)
    # A stale but complete cache refreshes from page 0 only and stays complete.
    idx = opts.source_root / "readingroom" / "throughput-index.json"
    data = read_json(idx)
    data["fetched_at"] = 0
    write_json(idx, data)
    again = reports.load_index(opts, D(2016, 1, 1))
    assert [r.path for r in again] == [r.path for r in full]


def test_load_index_stops_at_back_to(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    shutil.copytree(FIXTURES, mirror)
    for p in (mirror / "foia/readingroom").glob("page-*.html"):
        if p.name not in {"page-0.html", "page-1.html"}:
            p.unlink()  # a query for recent weeks stops at the first page wholly older than it
    o = options_from({"cache_dir": str(tmp_path / "c"), "base_url": str(mirror)})
    recent = reports.load_index(o, D(2026, 9, 1))
    assert len(recent) == 40
    assert max(r.end_date for r in recent[20:] if r.end_date) < D(2026, 9, 1)
    with pytest.raises(FileNotFoundError):
        reports.load_index(o)  # the full listing needs page 2 onward


def _report(path: str, start: D, end: D) -> reports.Report:
    return reports.Report(path=path, title=path, start_date=start, end_date=end, listed_size_mb=None)


def test_select_reports_never_double_counts() -> None:
    three_weeks = _report("a.pdf", D(2017, 1, 15), D(2017, 2, 4))
    week1 = _report("b.pdf", D(2017, 1, 15), D(2017, 1, 21))
    week2 = _report("c.pdf", D(2017, 1, 22), D(2017, 1, 28))
    plan = reports.select_reports([week2, three_weeks, week1], D(2017, 1, 20), D(2017, 2, 1))
    assert [(r.path, a, b) for r, a, b in plan] == [
        ("b.pdf", D(2017, 1, 20), D(2017, 1, 21)),
        ("c.pdf", D(2017, 1, 22), D(2017, 1, 28)),
        ("a.pdf", D(2017, 1, 29), D(2017, 2, 1)),
    ]


# -------------------------------------------------------------- PDF parse


def test_parse_pdf_2026_layout() -> None:
    result = parse_pdf(PDF_DIR / "tsa-throughput-data-to-november-1-2026-to-november-7-2026.pdf")
    assert (result.pages, result.pages_without_table, len(result.rejects)) == (5, 0, 0)
    assert len(result.rows) == 194
    first = result.rows[0]
    assert (first.date, first.hour, first.airport, first.state, first.checkpoint, first.throughput) == (
        D(2026, 11, 1),
        0,
        "ANC",
        "AK",
        "South Checkpoint",
        242,
    )
    mdw = {r.airport_name for r in result.rows if r.airport == "MDW"}
    assert mdw == {"Chicago Midway International"}  # "MD" + "W" wrapped in the cell
    multi = {r.checkpoint for r in result.rows if r.airport == "IAD"}
    assert "Passenger Screening Area: PreCheck" in multi  # a two-line cell
    assert result.rows[-1].date == D(2026, 11, 7) and result.rows[-1].hour == 23


def test_parse_pdf_2021_layout() -> None:
    result = parse_pdf(PDF_DIR / "tsa-throughput-december-5-2021-to-december-11-2021.pdf")
    assert (result.pages, len(result.rows), len(result.rejects)) == (3, 140, 0)
    assert {r.date for r in result.rows} == {D(2021, 12, 5), D(2021, 12, 11)}
    assert all(r.city is None or not r.city.endswith(",") for r in result.rows)


def test_validate_reports_reasons() -> None:
    good: dict[str, str | None] = {
        "date": "11/1/2026",
        "hour": "05:00",
        "airport": "ATL",
        "airport_name": "x",
        "city": "Atlanta",
        "state": "GA",
        "checkpoint": "Main",
        "throughput": "1,234",
    }
    row = _validate(good)
    assert not isinstance(row, str) and row.throughput == 1234 and row.hour == 5
    assert _validate({**good, "hour": "24:00"}) == "bad hour '24:00'"
    assert _validate({**good, "airport": "ATLX"}) == "bad airport code 'ATLX'"
    assert _validate({**good, "date": "13/1/2026"}) == "bad date '13/1/2026'"
    assert _validate({**good, "throughput": "12a"}) == "bad count '12a'"
    assert _validate({**good, "state": None}) == "bad state None"


def test_ensure_parsed_caches_parquet(opts: Options) -> None:
    idx = reports.load_index(opts)
    ((report, _, _),) = reports.select_reports(idx, D(2026, 11, 1), D(2026, 11, 7))
    table, rejects, meta = reports.ensure_parsed(opts, report)
    assert table.num_rows == meta["rows"] == 194 and rejects == []
    assert not list((opts.source_root / "throughput").glob("*.pdf"))  # PDF removed after parsing
    assert reports.parsed_meta(opts, report) is not None
    again, _, meta2 = reports.ensure_parsed(opts, report)
    assert again.equals(table) and meta2["parsed_at"] == meta["parsed_at"]


# ------------------------------------------------------------- via client


def _table(client: Client, name: str, *positional: Any, **named: Any) -> pa.Table:
    arguments = Arguments(
        positional=tuple(pa.scalar(v) for v in positional),
        named={k: pa.scalar(v) for k, v in named.items()},
    )
    batches = list(client.table_function(function_name=name, schema_path=["main"], arguments=arguments))
    return pa.Table.from_batches(batches)


def test_checkpoint_throughput_rejects_bad_airport(client: Client) -> None:
    with pytest.raises(Exception, match="pattern"):
        _table(client, "checkpoint_throughput", D(2026, 11, 1), D(2026, 11, 7), airport="ATLX")
