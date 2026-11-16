# Data sources

Upstream data licences are separate from the code licence (0BSD). Everything was retrieved on
2026-11-15 with the User-Agent `vgi-tsa/0.1 (+https://query.farm)`. tsa.gov answered every
request with HTTP 200; no bot protection or 403 was seen for that generic User-Agent.

All sources are publications of the Transportation Security Administration, a U.S. federal
agency: works of the U.S. government, public domain in the United States (17 U.S.C. 105).

## Fetched at runtime (never bundled)

| Data | URL | Notes |
|---|---|---|
| National daily checkpoint travel numbers, current year | https://www.tsa.gov/travel/passenger-volumes | HTML table `Date` / `Numbers`. On 2026-11-15 it ran 1/1/2026 to 11/12/2026 (316 rows): three days behind |
| Same, earlier years | `https://www.tsa.gov/travel/passenger-volumes/<year>` for 2019-2025 | One page per year, 365/366 rows each |
| FOIA reading room index, category "Throughput Data" | `https://www.tsa.gov/foia/readingroom?field_foia_tax_category_target_id=1132&page=<n>` | 27 pages (0-26) of 20 links. 536 links, 517 of them throughput PDFs; the rest (firearms, complaints, staffing...) are skipped |
| Weekly hourly throughput reports | `https://www.tsa.gov/sites/default/files/foia-readingroom/<file>.pdf` | ~4.5-6 MB, ~850-1,400 pages each; one Sunday-Saturday week per file |

Reading-room coverage found on 2026-11-15: still published after early 2024 (weekly through
2024, 2025 and 2026). Latest week: **1-7 November 2026**
(`tsa-throughput-data-to-november-1-2026-to-november-7-2026.pdf`, posted by 2026-11-09).
Earliest: 4-10 January 2015, then continuous from 15 January 2017. Gaps between consecutive
reports: 2015-01-11..20, 2015-01-28..2017-01-14, 2017-02-26..03-04, 2017-03-26..04-01,
2020-12-27..2021-01-02, 2022-07-02..07-09, 2025-08-03..08-09, 2025-10-17..10-18 (several of these
are covered by files posted out of order or with typo'd titles; the worker reads dates from the
file name when a title is wrong).

Upstream PDFs checked on 2026-11-15 (all parsed with zero rejected rows):

| File | Bytes | Pages | Rows | SHA-256 |
|---|---|---|---|---|
| tsa-throughput-data-to-november-1-2026-to-november-7-2026.pdf | 5,150,275 | 1,365 | 53,499 | f7b0bc499346ab65dc026e39a788cc3450bff1887e7bdaac4347909deb9bfef4 |
| tsa-total-throughput-data-october-11-2026-to-october-17-2026.pdf | 12,195,487 | 1,386 | 54,358 | 7f8e80f63dc0f9a02390df325bce6584c1edc8fe6e9327d2ef9f66fbefb789ae |
| tsa-throughput-december-5-2021-to-december-11-2021.pdf | 5,879,416 | 842 | 50,819 | 35ed6dc34d7c04394eccc4935e77944bb068ad1d557f070239057d85556478eb |
| tsa_throughput_february_12-18_2017.pdf | 4,701,360 | 908 | 55,289 | 4a6018bbca08fcdcd52762bea48968a9ca8ae22ec3fbf0ac333cb8e47329146d |

## Not available

- **MyTSA web service** (`https://apps.tsa.dhs.gov/MyTSAWebService/GetConfirmedWaitTimes?ap=ATL&output=json`,
  `GetTSOWaitTimes.ashx`, `GetAirportCheckpoints`): every request answers `302 Moved Temporarily`
  to `http://www.tsa.gov` (probed three times on 2026-11-15). The documentation page
  https://www.dhs.gov/mytsa-api-documentation redirects to the DHS archive. Not offered.

## Test fixtures (`test/fixtures/tsa`, 0.9 MB, built by `scripts/make_fixtures.py`)

A trimmed mirror of the files above, retrieved 2026-11-15, same public-domain status:

- `travel/passenger-volumes/index.html` and `travel/passenger-volumes/<2019..2025>/index.html`:
  the original pages cut down to the data table and the year links.
- `foia/readingroom/page-<0..26>.html`: the 27 index pages cut down to the listing table and pager.
- `sites/default/files/foia-readingroom/`:
  - `tsa-throughput-data-to-november-1-2026-to-november-7-2026.pdf`: pages 1, 2, 3, 701 and 1365
    of the original (SHA-256 above); fixture SHA-256
    19dcb1f0a8e8daaa497d0e1f7e3a93eeec0e45195cf428fb75ae7bbb05103c73.
  - `tsa-throughput-december-5-2021-to-december-11-2021.pdf`: pages 1, 2 and 842; fixture SHA-256
    55faf5b6530f451a5a990c823d59a40fac5b7da7697be90014723e9f56f3c51a.
