"""Table functions and table scans of the tsa catalog.

* ``daily_throughput``: national daily travelers screened (table; date filters pick the years fetched).
* ``throughput_reports``: the weekly FOIA reports and their date ranges (table).
* ``checkpoint_throughput(start_date, end_date, airport := , state := )``: hourly rows per checkpoint.
* ``checkpoint_throughput_rejects(start_date, end_date)``: report rows that failed validation.
* ``cache_info``: what is cached (table).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
import pyarrow.compute as pc
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import (
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .common import Options, options_from, read_json
from .meta import READING_ROOM_URL, VOLUMES_URL, column, object_tags, result_columns
from .reports import Report, cached_reports, ensure_parsed, load_index, parsed_meta, select_reports
from .volumes import FIRST_YEAR, rows_for_years

DATE = pa.date32()


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """No arguments."""


def _emit(out: OutputCollector, schema: pa.Schema, cols: dict[str, Any]) -> None:
    arrays = []
    for f in schema:
        col = cols[f.name]
        if isinstance(col, pa.ChunkedArray):
            col = col.combine_chunks()
        if not isinstance(col, pa.Array):
            col = pa.array(col, f.type)
        arrays.append(col.cast(f.type) if col.type != f.type else col)
    out.emit(pa.RecordBatch.from_arrays(arrays, schema=schema))


def _bound(pushed: Any, name: str) -> tuple[dt.date | None, dt.date | None]:
    """Inclusive ``(low, high)`` date bounds a pushed WHERE clause puts on ``name``."""
    if pushed is None:
        return None, None
    bounds = pushed.get_column_bounds(name)
    if bounds is None:
        return None, None

    def as_date(s: Any) -> dt.date | None:
        v = s.as_py() if s is not None else None
        if isinstance(v, dt.datetime):
            return v.date()
        return v if isinstance(v, dt.date) else None

    return as_date(bounds.min_value), as_date(bounds.max_value)


# ================================================================ daily_throughput

DAILY_SCHEMA = pa.schema(
    [
        column("date", DATE, "Calendar day (one row per day, from 2019-01-01).", nullable=False),
        column(
            "travelers",
            pa.int64(),
            "Travelers screened at TSA checkpoints nationwide that day, as published by TSA.",
            nullable=False,
        ),
    ]
)


@dataclass(kw_only=True)
class YearCursor(ArrowSerializableDataclass):
    """Cursor: the years still to emit, comma-separated (one year per batch)."""

    years: str = ""
    started: bool = False


@init_single_worker
@bind_fixed_schema
class DailyThroughput(TableFunctionGenerator[NoArgs, YearCursor]):
    """National daily traveler counts from tsa.gov/travel/passenger-volumes."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = DAILY_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "daily_throughput_scan"
        description = "Travelers screened at TSA checkpoints nationwide per day since 2019, posted 1-3 days late"
        filter_pushdown = True
        auto_apply_filters = True
        tags = {
            **object_tags(
                title="National Daily TSA Throughput",
                category="volumes",
                keywords="tsa, passenger volumes, daily travelers, checkpoint numbers, air travel demand",
                doc_llm=(
                    "One row per day since 2019-01-01: date (`DATE`) and travelers (`BIGINT`), the number of "
                    "people TSA screened at checkpoints nationwide, as listed on tsa.gov/travel/passenger-"
                    "volumes. TSA updates it on weekdays, usually 1-3 days behind, so the newest dates are "
                    "missing and recent days can be revised; it is not real time. Comparisons on date "
                    "(=, <, <=, >, >=, BETWEEN) are pushed down and decide which yearly pages are fetched; "
                    "use them instead of year(date) for speed. The current year is cached for 3 hours, past "
                    "years for 30 days. For hourly or per-airport detail use checkpoint_throughput()."
                ),
                doc_md=(
                    "## daily_throughput\n\n"
                    f"TSA's [checkpoint travel numbers]({VOLUMES_URL}): travelers screened nationwide per "
                    "day since 2019. Posted with a lag of 1-3 days. Filters on `date` are pushed down."
                ),
            ),
            "vgi.source_url": VOLUMES_URL,
            "vgi.result_columns_schema": result_columns(DAILY_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "The three busiest days of 2024",
                        "sql": (
                            "SELECT date, travelers FROM tsa.main.daily_throughput "
                            "WHERE date BETWEEN DATE '2024-01-01' AND DATE '2024-12-31' "
                            "ORDER BY travelers DESC LIMIT 3"
                        ),
                        "expected_result": [
                            ["2024-12-01", 3088836],
                            ["2024-07-07", 3013622],
                            ["2024-06-23", 2996495],
                        ],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT year(date) AS year, sum(travelers) AS travelers FROM tsa.main.daily_throughput "
                    "WHERE date < DATE '2026-01-01' GROUP BY ALL ORDER BY year"
                ),
                description="Travelers screened per calendar year, 2019 through 2025",
            ),
            FunctionExample(
                sql=(
                    "SELECT date, travelers, round(avg(travelers) OVER (ORDER BY date ROWS 6 PRECEDING)) "
                    "AS seven_day_avg FROM tsa.main.daily_throughput WHERE date >= DATE '2025-11-01' "
                    "AND date < DATE '2025-12-15' ORDER BY date"
                ),
                description="Daily travelers around Thanksgiving 2025 with a seven-day moving average",
            ),
        ]

    @classmethod
    def initial_state(cls, params: ProcessParams[NoArgs]) -> YearCursor:
        """Work out which years the pushed date filters need."""
        lo, hi = _bound(params.current_pushdown_filters, "date")
        today = dt.date.today()
        first = max(lo.year if lo else FIRST_YEAR, FIRST_YEAR)
        last = min(hi.year if hi else today.year, today.year)
        return YearCursor(years=",".join(str(y) for y in range(first, last + 1)))

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: YearCursor, out: OutputCollector) -> None:
        """Emit one year per call."""
        years = [int(y) for y in state.years.split(",") if y]
        if not years:
            out.finish()
            return
        year, rest = years[0], years[1:]
        state.years = ",".join(str(y) for y in rest)
        opts = options_from(params.attach_options)
        rows = rows_for_years(opts, {year})
        _emit(out, params.output_schema, {"date": [r[0] for r in rows], "travelers": [r[1] for r in rows]})


# ============================================================== throughput_reports

REPORTS_SCHEMA = pa.schema(
    [
        column("start_date", DATE, "First day the report covers."),
        column("end_date", DATE, "Last day the report covers (usually start_date + 6, Sunday to Saturday)."),
        column("title", pa.string(), "Title as listed in the FOIA reading room.", nullable=False),
        column("url", pa.string(), "URL of the report PDF on tsa.gov (unique per row).", nullable=False),
        column("listed_size_mb", pa.float64(), "PDF size shown in the reading room, in MB."),
        column("cached", pa.bool_(), "True when the report is already parsed in the local cache.", nullable=False),
        column("parsed_rows", pa.int64(), "Hourly rows parsed from the report (NULL until parsed)."),
        column("rejected_rows", pa.int64(), "Rows that failed validation (NULL until parsed)."),
    ]
)


@init_single_worker
@bind_fixed_schema
class ThroughputReports(TableFunctionGenerator[NoArgs, None]):
    """The weekly throughput reports listed in TSA's FOIA reading room."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = REPORTS_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "throughput_reports_scan"
        description = "Weekly TSA throughput reports in the FOIA reading room, with the dates each covers"
        filter_pushdown = True
        auto_apply_filters = True
        tags = {
            **object_tags(
                title="TSA Throughput Reports",
                category="checkpoints",
                keywords="tsa, foia, reading room, weekly reports, throughput pdf, report dates, coverage",
                doc_llm=(
                    "One row per weekly 'TSA Throughput' PDF in TSA's FOIA reading room (about 510 weeks, "
                    "2017 onward, plus two January 2015 files), newest posting first in the source. "
                    "Columns: start_date and end_date (the week covered, parsed from the title; NULL if the "
                    "title cannot be read), title, url, listed_size_mb, cached (already parsed locally), "
                    "parsed_rows and rejected_rows (NULL until parsed). Use max(end_date) to find the latest "
                    "week checkpoint_throughput() can return; TSA posts each week one to two weeks after it "
                    "ends, and some weeks are missing. A filter like `end_date >= DATE '2026-01-01'` is "
                    "pushed down and stops paging through the index early; the full listing reads all ~27 "
                    "index pages once, then is cached for 6 hours."
                ),
                doc_md=(
                    "## throughput_reports\n\n"
                    f"The weekly throughput PDFs in TSA's [FOIA reading room]({READING_ROOM_URL}) "
                    "(category *Throughput Data*), with the dates each one covers and whether it is cached."
                ),
            ),
            "vgi.source_url": READING_ROOM_URL,
            "vgi.result_columns_schema": result_columns(REPORTS_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "The first three weekly reports TSA posted in the reading room",
                        "sql": (
                            "SELECT start_date, end_date FROM tsa.main.throughput_reports "
                            "WHERE start_date IS NOT NULL ORDER BY start_date LIMIT 3"
                        ),
                        "expected_result": [
                            ["2015-01-04", "2015-01-10"],
                            ["2015-01-21", "2015-01-27"],
                            ["2017-01-15", "2017-01-21"],
                        ],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT start_date, end_date, title FROM tsa.main.throughput_reports "
                    "WHERE end_date >= DATE '2026-09-01' ORDER BY end_date DESC LIMIT 5"
                ),
                description="The five most recent weekly checkpoint reports",
            ),
            FunctionExample(
                sql=(
                    "SELECT year(start_date) AS year, count(*) AS weeks FROM tsa.main.throughput_reports "
                    "WHERE start_date IS NOT NULL GROUP BY ALL ORDER BY year"
                ),
                description="How many weekly reports exist for each year",
            ),
        ]

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: None, out: OutputCollector) -> None:
        """List the reports (paging the index only as far back as pushed date filters need)."""
        opts = options_from(params.attach_options)
        pushed = params.current_pushdown_filters
        lows = [b for b in (_bound(pushed, "end_date")[0], _bound(pushed, "start_date")[0]) if b is not None]
        back_to = max(lows) - dt.timedelta(days=7) if lows else None
        reports = load_index(opts, back_to)
        metas = [parsed_meta(opts, r) for r in reports]
        _emit(
            out,
            params.output_schema,
            {
                "start_date": [r.start_date for r in reports],
                "end_date": [r.end_date for r in reports],
                "title": [r.title for r in reports],
                "url": [r.url for r in reports],
                "listed_size_mb": [r.listed_size_mb for r in reports],
                "cached": [m is not None for m in metas],
                "parsed_rows": [m["rows"] if m else None for m in metas],
                "rejected_rows": [m["rejected_rows"] if m else None for m in metas],
            },
        )
        out.finish()


# =========================================================== checkpoint_throughput

CHECKPOINT_SCHEMA = pa.schema(
    [
        column("date", DATE, "Day of the count.", nullable=False),
        column(
            "hour",
            pa.int32(),
            "Hour of day 0-23 as printed by TSA (local airport time); the row counts that hour.",
            nullable=False,
        ),
        column("airport", pa.string(), "3-letter IATA airport code, e.g. 'ATL'.", nullable=False),
        column("airport_name", pa.string(), "Airport name as TSA lists it."),
        column("city", pa.string(), "City TSA lists for the airport."),
        column("state", pa.string(), "2-letter USPS state or territory code, e.g. 'GA', 'PR', 'GU'.", nullable=False),
        column("checkpoint", pa.string(), "Checkpoint name as TSA lists it (names can be truncated by TSA)."),
        column(
            "throughput",
            pa.int64(),
            "People screened at that checkpoint in that hour. 2023+ reports count passengers plus crew; "
            "earlier reports count customers (unadjusted).",
            nullable=False,
        ),
        column("report_url", pa.string(), "URL of the weekly PDF the row was parsed from.", nullable=False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class CheckpointArgs:
    """Arguments for ``checkpoint_throughput(start_date, end_date, airport := , state := )``."""

    start_date: Annotated[dt.date, Arg(0, doc="First day to return, e.g. '2026-11-01'.", arrow_type=DATE)]
    end_date: Annotated[dt.date, Arg(1, doc="Last day to return (inclusive).", arrow_type=DATE)]
    airport: Annotated[
        str | None,
        Arg(
            "airport",
            default=None,
            doc="Only this 3-letter IATA airport code, e.g. 'JFK'.",
            pattern=r"^[A-Za-z0-9]{3}$",
        ),
    ]
    state: Annotated[
        str | None,
        Arg(
            "state",
            default=None,
            doc="Only airports in this 2-letter USPS state code, e.g. 'CA'.",
            pattern=r"^[A-Za-z]{2}$",
        ),
    ]


@dataclass(kw_only=True)
class ReportCursor(ArrowSerializableDataclass):
    """Cursor: the report PDFs (tsa.gov-relative paths) still to read, newline-separated."""

    remaining: str = ""


def _plan(opts: Options, start: dt.date, end: dt.date) -> list[tuple[Report, dt.date, dt.date]]:
    """Pick the reports for a date range and enforce the per-query download limit."""
    if end < start:
        raise ValueError(f"end_date {end} is before start_date {start}")
    plan = select_reports(load_index(opts, start), start, end)
    if not plan:
        raise ValueError(
            f"no TSA throughput report covers {start} to {end}. Weekly reports are posted one to two weeks "
            "after the week ends and some weeks are missing; see tsa.main.throughput_reports."
        )
    uncached = [r for r, _, _ in plan if parsed_meta(opts, r) is None]
    if len(uncached) > opts.max_reports:
        raise ValueError(
            f"{start} to {end} needs {len(uncached)} weekly reports that are not cached yet (~5 MB and ~15 s "
            f"each), more than max_reports = {opts.max_reports}. Query a shorter range, or ATTACH with a larger "
            f"limit, e.g. ATTACH 'tsa' (TYPE vgi, LOCATION '...', max_reports {len(uncached)})."
        )
    return plan


def _span(plan: list[tuple[Report, dt.date, dt.date]], path: str) -> tuple[Report, dt.date, dt.date]:
    return next(t for t in plan if t[0].path == path)


@init_single_worker
@bind_fixed_schema
class CheckpointThroughput(TableFunctionGenerator[CheckpointArgs, ReportCursor]):
    """Hourly passengers per checkpoint, parsed from the weekly FOIA reports."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CHECKPOINT_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "checkpoint_throughput"
        description = "Hourly passengers screened per airport checkpoint from TSA's weekly FOIA reports (weeks late)"
        tags = {
            **object_tags(
                title="Hourly Checkpoint Throughput",
                category="checkpoints",
                keywords="tsa, checkpoint, airport, hourly passengers, throughput, security lines, foia, iata",
                doc_llm=(
                    "checkpoint_throughput(start_date `DATE`, end_date `DATE`, airport := 'ATL', state := 'GA') "
                    "returns one row per date, hour (0-23, local airport time as printed), airport (IATA), "
                    "checkpoint, with airport_name, city, state (USPS), throughput (people screened that "
                    "hour) and report_url. Data comes from the weekly 'TSA Throughput' PDFs in TSA's FOIA "
                    "reading room (2017 onward, about 420 airports): the newest week is usually one to two "
                    "weeks old, so check max(end_date) in throughput_reports first. Each week not yet "
                    "cached is downloaded and parsed on first use (~5 MB, ~15 s); one query may need at "
                    "most max_reports uncached weeks (ATTACH option, default 8), otherwise it fails with "
                    "a message saying how many it needs. airport and state filter rows before they leave "
                    "the worker. Rows whose cells fail validation are left out and counted in "
                    "throughput_reports.rejected_rows; see them with checkpoint_throughput_rejects()."
                ),
                doc_md=(
                    "## checkpoint_throughput(start_date, end_date, airport := , state := )\n\n"
                    "Hourly passengers screened per checkpoint, parsed from TSA's weekly "
                    f"[FOIA throughput reports]({READING_ROOM_URL}). Reports appear one to two weeks after "
                    "the week ends. The first query for a week downloads and parses its PDF (~15 s); "
                    "`max_reports` (ATTACH option, default 8) bounds how many a query may fetch."
                ),
            ),
            "vgi.source_url": READING_ROOM_URL,
            "vgi.result_columns_schema": result_columns(CHECKPOINT_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Passengers screened per Atlanta checkpoint at midnight on 1 November 2026",
                        "sql": (
                            "SELECT checkpoint, throughput FROM tsa.main.checkpoint_throughput("
                            "DATE '2026-11-01', DATE '2026-11-01', airport := 'ATL') WHERE hour = 0 "
                            "ORDER BY checkpoint"
                        ),
                        "expected_result": [["F Arrival Checkpoint", 7], ["Main Checkpoint", 35]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT airport, sum(throughput) AS passengers FROM tsa.main.checkpoint_throughput("
                    "DATE '2026-11-01', DATE '2026-11-07') GROUP BY airport ORDER BY passengers DESC LIMIT 10"
                ),
                description="The ten airports that screened the most passengers in the week of 1 November 2026",
            ),
            FunctionExample(
                sql=(
                    "SELECT hour, sum(throughput) AS passengers FROM tsa.main.checkpoint_throughput("
                    "DATE '2026-11-01', DATE '2026-11-07', airport := 'JFK') GROUP BY hour ORDER BY hour"
                ),
                description="JFK's passengers by hour of day over one week",
            ),
        ]

    @classmethod
    def initial_state(cls, params: ProcessParams[CheckpointArgs]) -> ReportCursor:
        """Choose the weekly reports and check the download limit (nothing is downloaded yet)."""
        a = params.args
        plan = _plan(options_from(params.attach_options), a.start_date, a.end_date)
        return ReportCursor(remaining="\n".join(r.path for r, _, _ in plan))

    @classmethod
    def process(cls, params: ProcessParams[CheckpointArgs], state: ReportCursor, out: OutputCollector) -> None:
        """Read (downloading and parsing if needed) one report per call and emit its matching rows."""
        paths = [p for p in state.remaining.split("\n") if p]
        if not paths:
            out.finish()
            return
        state.remaining = "\n".join(paths[1:])
        a = params.args
        opts = options_from(params.attach_options)
        report, first, last = _span(select_reports(load_index(opts, a.start_date), a.start_date, a.end_date), paths[0])
        table, _, _ = ensure_parsed(opts, report)
        mask = pc.and_(
            pc.greater_equal(table["date"], pa.scalar(first, DATE)), pc.less_equal(table["date"], pa.scalar(last, DATE))
        )
        if a.airport:
            mask = pc.and_(mask, pc.equal(table["airport"], pa.scalar(a.airport.upper())))
        if a.state:
            mask = pc.and_(mask, pc.equal(table["state"], pa.scalar(a.state.upper())))
        rows = table.filter(mask)
        cols: dict[str, Any] = {name: rows[name] for name in rows.column_names}
        cols["report_url"] = pa.array([report.url] * rows.num_rows, pa.string())
        _emit(out, params.output_schema, cols)


# ==================================================== checkpoint_throughput_rejects

REJECTS_SCHEMA = pa.schema(
    [
        column("report_url", pa.string(), "URL of the weekly PDF.", nullable=False),
        column("page", pa.int32(), "1-based PDF page the row is on.", nullable=False),
        column("reason", pa.string(), "Which check failed, e.g. \"bad hour '24:00'\".", nullable=False),
        column("date_text", pa.string(), "Raw Date cell."),
        column("hour_text", pa.string(), "Raw Hour of Day cell."),
        column("airport_text", pa.string(), "Raw airport code cell."),
        column("airport_name", pa.string(), "Raw airport name cell."),
        column("city", pa.string(), "Raw City cell."),
        column("state_text", pa.string(), "Raw State cell."),
        column("checkpoint", pa.string(), "Raw Checkpoint cell."),
        column("throughput_text", pa.string(), "Raw count cell."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class RejectArgs:
    """Arguments for ``checkpoint_throughput_rejects(start_date, end_date)``."""

    start_date: Annotated[dt.date, Arg(0, doc="First day whose reports to inspect.", arrow_type=DATE)]
    end_date: Annotated[dt.date, Arg(1, doc="Last day whose reports to inspect (inclusive).", arrow_type=DATE)]


@init_single_worker
@bind_fixed_schema
class CheckpointThroughputRejects(TableFunctionGenerator[RejectArgs, ReportCursor]):
    """Rows of the weekly reports that failed validation."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = REJECTS_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "checkpoint_throughput_rejects"
        description = "Rows of TSA's weekly throughput reports that the parser could not validate, with raw cells"
        tags = {
            **object_tags(
                title="Checkpoint Throughput Parse Rejects",
                category="checkpoints",
                keywords="tsa, parse errors, rejected rows, data quality, throughput pdf, validation",
                doc_llm=(
                    "checkpoint_throughput_rejects(start_date `DATE`, end_date `DATE`) reads the same weekly "
                    "reports as checkpoint_throughput() (same download limit) and returns the table rows "
                    "that failed validation (date not M/D/YYYY, hour outside 0-23, airport code not 3 "
                    "characters, state not 2 letters, count not an integer), with the PDF page, the "
                    "reason and every raw cell as text. Normally empty; use it to audit parse quality."
                ),
                doc_md=(
                    "## checkpoint_throughput_rejects(start_date, end_date)\n\n"
                    "Rows the PDF parser could not validate, so they are never dropped silently."
                ),
            ),
            "vgi.source_url": READING_ROOM_URL,
            "vgi.result_columns_schema": result_columns(REJECTS_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Rejected rows in the report for 1-7 November 2026 (none)",
                        "sql": (
                            "SELECT count(*) AS rejects FROM tsa.main.checkpoint_throughput_rejects("
                            "DATE '2026-11-01', DATE '2026-11-07')"
                        ),
                        "expected_result": [[0]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT count(*) AS rejects, count(DISTINCT reason) AS reasons, min(page) AS first_page "
                    "FROM tsa.main.checkpoint_throughput_rejects(DATE '2026-11-01', DATE '2026-11-07')"
                ),
                description="Confirm a weekly report parsed cleanly: 0 rejects means every row passed validation",
            ),
        ]

    @classmethod
    def initial_state(cls, params: ProcessParams[RejectArgs]) -> ReportCursor:
        """Choose the weekly reports and check the download limit."""
        a = params.args
        plan = _plan(options_from(params.attach_options), a.start_date, a.end_date)
        return ReportCursor(remaining="\n".join(r.path for r, _, _ in plan))

    @classmethod
    def process(cls, params: ProcessParams[RejectArgs], state: ReportCursor, out: OutputCollector) -> None:
        """Emit the rejects of one report per call."""
        paths = [p for p in state.remaining.split("\n") if p]
        if not paths:
            out.finish()
            return
        state.remaining = "\n".join(paths[1:])
        a = params.args
        opts = options_from(params.attach_options)
        report, _, _ = _span(select_reports(load_index(opts, a.start_date), a.start_date, a.end_date), paths[0])
        _, rejects, _ = ensure_parsed(opts, report)
        raw = [r.get("raw", {}) for r in rejects]
        _emit(
            out,
            params.output_schema,
            {
                "report_url": [report.url] * len(rejects),
                "page": [int(r["page"]) for r in rejects],
                "reason": [str(r["reason"]) for r in rejects],
                "date_text": [x.get("date") for x in raw],
                "hour_text": [x.get("hour") for x in raw],
                "airport_text": [x.get("airport") for x in raw],
                "airport_name": [x.get("airport_name") for x in raw],
                "city": [x.get("city") for x in raw],
                "state_text": [x.get("state") for x in raw],
                "checkpoint": [x.get("checkpoint") for x in raw],
                "throughput_text": [x.get("throughput") for x in raw],
            },
        )


# ======================================================================= cache_info

CACHE_SCHEMA = pa.schema(
    [
        column("kind", pa.string(), "daily_volumes, report_index or checkpoint_report.", nullable=False),
        column("path", pa.string(), "Local cache file (unique per row).", nullable=False),
        column("source_url", pa.string(), "Where the data came from."),
        column("start_date", DATE, "First day covered (checkpoint reports and yearly volume pages)."),
        column("end_date", DATE, "Last day covered."),
        column("rows", pa.int64(), "Count of data rows held in the file (days, reports or hourly rows)."),
        column("rejected_rows", pa.int64(), "Report rows that failed validation (checkpoint reports only)."),
        column("bytes", pa.int64(), "Size of the cache file in bytes."),
        column("sha256", pa.string(), "SHA-256 of the downloaded PDF (checkpoint reports only)."),
        column("fetched_at", pa.timestamp("us", tz="UTC"), "When it was downloaded or parsed."),
    ]
)


def _ts(v: Any) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(float(v), dt.UTC) if v is not None else None


def cache_rows(opts: Options) -> list[dict[str, Any]]:
    """Describe the cache as rows of `CACHE_SCHEMA`."""
    rows: list[dict[str, Any]] = []
    vol_dir = opts.source_root / "passenger-volumes"
    for p in sorted(vol_dir.glob("*.json")) if vol_dir.exists() else []:
        data = read_json(p)
        if not isinstance(data, dict) or not data.get("rows"):
            continue
        dates = [d for d, _ in data["rows"]]
        rows.append(
            {
                "kind": "daily_volumes",
                "path": str(p),
                "source_url": data.get("source_url"),
                "start_date": dt.date.fromisoformat(min(dates)),
                "end_date": dt.date.fromisoformat(max(dates)),
                "rows": len(dates),
                "rejected_rows": None,
                "bytes": p.stat().st_size,
                "sha256": None,
                "fetched_at": _ts(data.get("fetched_at")),
            }
        )
    idx = opts.source_root / "readingroom" / "throughput-index.json"
    data = read_json(idx)
    if isinstance(data, dict):
        rows.append(
            {
                "kind": "report_index",
                "path": str(idx),
                "source_url": data.get("source_url"),
                "start_date": None,
                "end_date": None,
                "rows": len(data.get("entries", [])),
                "rejected_rows": None,
                "bytes": idx.stat().st_size,
                "sha256": None,
                "fetched_at": _ts(data.get("fetched_at")),
            }
        )
    for meta in cached_reports(opts):
        p = meta["path"]
        size = None
        try:
            from pathlib import Path

            size = Path(p).stat().st_size
        except OSError:
            size = None
        rows.append(
            {
                "kind": "checkpoint_report",
                "path": p,
                "source_url": meta.get("source_url"),
                "start_date": dt.date.fromisoformat(meta["start_date"]) if meta.get("start_date") else None,
                "end_date": dt.date.fromisoformat(meta["end_date"]) if meta.get("end_date") else None,
                "rows": meta.get("rows"),
                "rejected_rows": meta.get("rejected_rows"),
                "bytes": size,
                "sha256": meta.get("sha256"),
                "fetched_at": _ts(meta.get("parsed_at")),
            }
        )
    return rows


@init_single_worker
@bind_fixed_schema
class CacheInfo(TableFunctionGenerator[NoArgs, None]):
    """What the worker has cached on disk."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CACHE_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "cache_info_scan"
        description = "Files in the local cache: daily volume pages, the report index and parsed weekly reports"
        tags = {
            **object_tags(
                title="Cache Contents",
                category="cache",
                keywords="cache, downloads, parsed reports, freshness, provenance, sha256",
                doc_llm=(
                    "One row per cached file: kind ('daily_volumes' per year page, 'report_index', "
                    "'checkpoint_report' per parsed weekly PDF), path, source_url, start_date/end_date "
                    "covered, rows, rejected_rows, bytes, sha256 of the PDF and fetched_at. Reading it "
                    "never touches the network. The cache lives in the cache_dir ATTACH option, by default "
                    "the per-user cache directory (e.g. ~/Library/Caches/vgi-tsa)."
                ),
                doc_md=("## cache_info\n\nWhat has been downloaded and parsed, with provenance and checksums."),
            ),
            "vgi.source_url": READING_ROOM_URL,
            "vgi.result_columns_schema": result_columns(CACHE_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT count(*) AS files, count(*) FILTER (WHERE kind = 'checkpoint_report') AS weeks, "
                    "coalesce(sum(bytes), 0) AS bytes FROM tsa.main.cache_info"
                ),
                description="How many files, parsed weeks and bytes are cached",
            ),
            FunctionExample(
                sql=(
                    "SELECT count(*) AS weeks, coalesce(sum(rows), 0) AS hourly_rows, "
                    "coalesce(sum(rejected_rows), 0) AS rejected FROM tsa.main.cache_info "
                    "WHERE kind = 'checkpoint_report'"
                ),
                description="Weekly reports parsed so far, with their row and reject totals",
            ),
        ]

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: None, out: OutputCollector) -> None:
        """Emit the cache listing."""
        rows = cache_rows(options_from(params.attach_options))
        _emit(out, params.output_schema, {f.name: [r[f.name] for r in rows] for f in CACHE_SCHEMA})
        out.finish()
