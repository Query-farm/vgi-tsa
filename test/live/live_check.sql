-- Opt-in live check against tsa.gov. Run with `make live-check` (never part of `make check`).
SELECT max(date) AS latest_day, arg_max(travelers, date) AS travelers FROM tsa.main.daily_throughput WHERE date >= current_date - 14;
SELECT max(end_date) AS latest_report_week_end, count(*) AS recent_reports FROM tsa.main.throughput_reports WHERE end_date >= current_date - 60;
SELECT airport, sum(throughput) AS passengers FROM tsa.main.checkpoint_throughput(DATE '2026-11-01', DATE '2026-11-07')
GROUP BY airport ORDER BY passengers DESC LIMIT 5;
SELECT kind, count(*) AS files, sum(rows) AS rows, sum(rejected_rows) AS rejected FROM tsa.main.cache_info GROUP BY kind ORDER BY kind;
