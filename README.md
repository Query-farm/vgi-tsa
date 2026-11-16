<p align="center">
  <img src="https://query.farm/media-kit/products/vgi/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

# TSA Checkpoint Passenger Volumes by Day, Airport and Hour in DuckDB

> **vgi-tsa** · a [Query.Farm](https://query.farm) VGI worker

[![vgi worker: query.farm](https://query.farm/media-kit/shields/vgi-worker.svg)](https://query.farm)

How many people the Transportation Security Administration screens, as SQL: the nationwide
count for every day since 2019, and passengers per **hour** at each **checkpoint** of about 420
US airports, parsed from the weekly throughput reports TSA posts in its FOIA reading room.
No API key.

**Nothing here is real time.** TSA posts the national daily count with a lag of one to three
days. The hourly checkpoint reports are weekly PDFs that appear one to two weeks after the week
ends, with occasional gaps.

```sql
-- Thanksgiving week, nationwide, against the same days a year earlier.
SELECT date, travelers,
       lag(travelers, 364) OVER (ORDER BY date) AS same_weekday_last_year
FROM tsa.main.daily_throughput
WHERE date >= DATE '2024-11-01'
QUALIFY date >= DATE '2025-11-20' AND date < DATE '2025-12-03'
ORDER BY date;

-- Which weeks of hourly data exist?
SELECT start_date, end_date FROM tsa.main.throughput_reports ORDER BY end_date DESC LIMIT 3;

-- When are JFK's checkpoints busiest? (downloads and parses one ~5 MB PDF on first use)
SELECT hour, sum(throughput) AS passengers
FROM tsa.main.checkpoint_throughput(DATE '2026-11-01', DATE '2026-11-07', airport := 'JFK')
GROUP BY hour ORDER BY hour;
```

## Install and attach

Requires [uv](https://docs.astral.sh/uv/) and DuckDB 2.0 (or [Haybarn](https://query.farm/products/haybarn) 2.0).

```sql
-- DuckDB 2.0
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;

ATTACH 'tsa' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-tsa@v0.1.2 vgi-tsa-worker');
```

In Haybarn 2.0: `INSTALL vgi FROM community; LOAD vgi;` then the same `ATTACH`.
From a checkout: `make build`, then
`ATTACH 'tsa' (TYPE vgi, LOCATION '/abs/path/to/vgi-tsa/.venv/bin/vgi-tsa-worker')`.

### ATTACH options

| Option | Default | Meaning |
|---|---|---|
| `cache_dir` | per-user cache dir (`~/Library/Caches/vgi-tsa`, `~/.cache/vgi-tsa`, `%LOCALAPPDATA%\vgi-tsa`) | Cached pages and parsed weekly reports (about 1 MB of Parquet per week) |
| `max_reports` | `8` | Most weekly PDFs one query may download and parse (each ~5 MB, ~15 s). Cached weeks don't count. A query over the limit fails before downloading anything and says how many it needs |
| `base_url` | `https://www.tsa.gov` | A mirror, or a local directory laid out like tsa.gov (see `test/fixtures/tsa`) |
| `user_agent` | `vgi-tsa/0.1 (+https://query.farm)` | Sent with every request |
| `offline` | `false` | Never touch the network; use only the cache, even when stale |

## Tables and functions

| Object | What it returns |
|---|---|
| `daily_throughput` (table) | `date DATE`, `travelers BIGINT`: people screened at TSA checkpoints nationwide, one row per day since 2019-01-01 |
| `throughput_reports` (table) | Each weekly FOIA throughput PDF: `start_date`, `end_date`, `title`, `url`, `listed_size_mb`, whether it is `cached`, `parsed_rows`, `rejected_rows` |
| `checkpoint_throughput(start_date, end_date, airport := , state := )` | `date`, `hour`, `airport` (IATA), `airport_name`, `city`, `state` (USPS), `checkpoint`, `throughput`, `report_url` |
| `checkpoint_throughput_rejects(start_date, end_date)` | Report rows that failed validation, with the PDF page, the reason and every raw cell |
| `cache_info` (table) | Every cached file with its source URL, date span, row and reject counts, size, PDF SHA-256 and fetch time |

Join keys: `airport` is the 3-letter IATA code as `VARCHAR` (joins airport reference data such
as vgi-ourairports or vgi-faa-nasr on the IATA code), `state` the 2-letter USPS code
(territories included: PR, GU, VI, AS, MP), `date` a `DATE`, `hour` an `INTEGER` 0-23.

**Hours are local airport time.** TSA prints "Hour of Day" without a time zone. Every airport
checked (JFK, LAX, HNL, ANC, GUM) peaks in its own local morning and bottoms out in its own small
hours, which only fits local time.

**What the counts are.** `daily_throughput.travelers` is TSA's headline "travel numbers" figure.
`checkpoint_throughput.throughput` is TSA's per-checkpoint count: "Total Pax + Crew" in the 2023+
reports and "PMIS Total Customer Throughput (Unadjusted)" in the 2017-2022 ones. Summed over all
checkpoints in the report, a day comes to about 98% of the national figure (1 November 2026:
2,486,896 vs 2,530,793), so the reports do not cover every screening lane.

### Pushdown and caching

- `daily_throughput`: comparisons on `date` (`=`, `<`, `>`, `BETWEEN` ...) are pushed down and
  decide which yearly pages are fetched; the current-year page is always read (it lists the
  archive years). Write `date >= DATE '2024-01-01'`, not `year(date) = 2024`, to benefit. The
  current year is cached for 3 hours, finished years for 30 days.
- `throughput_reports`: a lower bound on `start_date` or `end_date` stops paging through the
  reading-room index early. The full listing reads ~27 index pages once (half a second apart),
  then is cached for 6 hours and refreshed incrementally.
- `checkpoint_throughput`: the date range picks the weekly PDFs; `airport` and `state` filter
  rows in the worker. Each PDF is downloaded once, parsed (about 15 seconds for ~1,400 pages),
  stored as Parquet and deleted. Where TSA posted overlapping files (a three-week report next to
  weekly ones), each day is read from exactly one file, so nothing double-counts.

### How the PDFs are parsed

TSA exports these reports from MicroStrategy as one long drawn table. Plain text extraction
jumbles the columns (date and hour cells are merged and vertically centred, narrow cells wrap
mid-token such as "MD" + "W"), so the parser reads the drawn cell rectangles with
[PDFium](https://pdfium.googlesource.com/pdfium/), assigns each text run to its cell, and lets
merged cells cover every row they span. Every row is validated (date, hour 0-23, 3-character
airport code, 2-letter state, integer count). Rows that fail are counted in
`throughput_reports.rejected_rows` / `cache_info.rejected_rows` and returned by
`checkpoint_throughput_rejects`, never dropped silently. Both layouts TSA has used parse; the
reports for 12-18 February 2017, 5-11 December 2021, 11-17 October 2026 and 1-7 November 2026
parsed with zero rejects (55,289 / 50,819 / 54,358 / 53,499 rows).

### Not included

- **Checkpoint wait times.** The MyTSA web service (`apps.tsa.dhs.gov/MyTSAWebService/...`)
  that used to serve live wait times now redirects every request to the tsa.gov home page, and
  its documentation is archived; there is no wait-time function.
- **Other FOIA reading-room documents** (firearms discoveries, complaints, ...) are skipped by
  `throughput_reports`.

## Data sources and terms

The code license (below) does not cover the data. All data is fetched from tsa.gov at runtime;
nothing is bundled except the small test fixtures. TSA publications are works of the U.S.
federal government and in the public domain in the United States. See
[DATA-SOURCES.md](DATA-SOURCES.md) for URLs, retrieval dates and checksums. This worker is not
affiliated with or endorsed by TSA or DHS.

## Development

```bash
make check                 # ruff, mypy, pydoclint, pytest, SQL tests over stdio/launch/http, vgi-lint
make test-unit
make test-sql TRANSPORT=stdio
make live-check            # opt-in: real tsa.gov (not part of check)
```

Tests and the lint run are offline. `test/fixtures/tsa` is a trimmed mirror of tsa.gov: the
eight passenger-volume pages and the 27 reading-room index pages cut down to their tables, and
two real report PDFs cut down to a few pages each (one per layout). The worker reads it through
the ordinary `base_url` option. Rebuild it with `scripts/make_fixtures.py`.

## Support

Open an issue on [GitHub](https://github.com/Query-farm/vgi-tsa/issues).

## Authorship & License

Copyright 2026 Query Farm LLC - https://query.farm

Released under the [BSD Zero Clause License](LICENSE) (0BSD). Data terms are separate; see
above.
