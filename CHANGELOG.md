# Changelog

## 0.1.1 (2026-11-16)

Fix: the download cache lock is now held by the operating system, so it is released when a worker is killed mid-download (e.g. Ctrl-C on the first query). Previously the leftover lock file made the next query wait up to 15 minutes. Abandoned partial downloads are removed on the next fetch.

## 0.1.0 (2026-11-16)

TSA checkpoint data: `daily_throughput`, `throughput_reports`, `checkpoint_throughput`,
`checkpoint_throughput_rejects` and `cache_info`, from tsa.gov's passenger-volumes pages and the
weekly FOIA throughput PDFs. Generated from vgi-starter-python v1.3.1.

Targets: vgi-python 0.51 · VGI protocol 3.3 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
