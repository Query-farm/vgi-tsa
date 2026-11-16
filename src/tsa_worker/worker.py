"""Assemble the functions into a catalog and run the worker.

Functions live in ``functions.py``; add new ones to ``FUNCTIONS`` (and, for
argument-free scans, ``TABLES``) below.

Run it:

    vgi-tsa-worker                      # stdio: what DuckDB launches by default
    vgi-tsa-worker --http --port 8000   # HTTP: for Docker / hosted deploys
    vgi-tsa-worker --help

Attach it from DuckDB:

    ATTACH 'tsa' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-tsa-worker');
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from vgi import Worker
from vgi.catalog import Catalog, Schema, Table
from vgi.catalog.attach_option import AttachOption
from vgi.catalog.constraints import constraint_record

from .common import DEFAULT_BASE_URL, DEFAULT_MAX_REPORTS, DEFAULT_USER_AGENT
from .functions import (
    CacheInfo,
    CheckpointThroughput,
    CheckpointThroughputRejects,
    DailyThroughput,
    ThroughputReports,
)
from .meta import (
    CATALOG_COMMENT,
    CATALOG_NAME,
    CATALOG_TAGS,
    SCHEMA_CATEGORIES,
    SCHEMA_COMMENT,
    SCHEMA_DOC_LLM,
    SCHEMA_DOC_MD,
    SOURCE_URL,
    keywords_array,
)

FUNCTIONS: list[type[Any]] = [CheckpointThroughput, CheckpointThroughputRejects]
TABLE_SCANS: list[tuple[type[Any], str, str | None]] = [
    (DailyThroughput, "daily_throughput", "date"),
    (ThroughputReports, "throughput_reports", "url"),
    (CacheInfo, "cache_info", "path"),
]


def _examples(fn: type[Any]) -> list[dict[str, str]]:
    return [{"description": e.description, "sql": e.sql} for e in getattr(fn.Meta, "examples", [])]


for _fn in FUNCTIONS:
    _fn.Meta.tags = {**_fn.Meta.tags, "vgi.example_queries": json.dumps(_examples(_fn))}


def _as_table(fn: type[Any], name: str, key: str | None) -> Table:
    """Expose an argument-free scan as a catalog table carrying the scan's metadata."""
    constraints: tuple[Any, ...] = ()
    if key is not None:
        constraints = (
            constraint_record(
                ["main"], name, f"{key}_not_null", "NOT_NULL", [[key]], enforcement="ENFORCED", validation="ALL_ROWS"
            ),
            constraint_record(
                ["main"], name, "pk", "PRIMARY_KEY", [[key]], enforcement="ENFORCED", validation="ALL_ROWS"
            ),
        )
    return Table(
        name=name,
        function=fn,
        comment=fn.Meta.description,
        tags={**fn.Meta.tags, "domain": "air travel", "vgi.example_queries": json.dumps(_examples(fn))},
        constraints=constraints,
        column_comments={
            f.name: (f.metadata or {}).get(b"comment", b"").decode() for f in fn.FIXED_SCHEMA if f.metadata
        },
    )


TABLES = [_as_table(fn, name, key) for fn, name, key in TABLE_SCANS]


class TsaWorker(Worker):
    """The worker process: one catalog, one ``main`` schema."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_COMMENT,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment=SCHEMA_COMMENT,
                functions=FUNCTIONS,
                tables=TABLES,
                tags={
                    "vgi.title": "TSA Checkpoint Data",
                    "domain": "air travel",
                    "vgi.keywords": keywords_array("tsa, checkpoint, airport, passenger volumes, throughput"),
                    "vgi.source_url": "https://www.tsa.gov/travel/passenger-volumes",
                    "vgi.doc_llm": SCHEMA_DOC_LLM,
                    "vgi.doc_md": SCHEMA_DOC_MD,
                    "vgi.categories": json.dumps(SCHEMA_CATEGORIES),
                    "vgi.example_queries": json.dumps(
                        [e for fn in [*FUNCTIONS, *(t[0] for t in TABLE_SCANS)] for e in _examples(fn)]
                    ),
                },
            )
        ],
    )

    class AttachOptions:
        """Options for ``ATTACH 'tsa' (TYPE vgi, LOCATION '...', cache_dir '...', ...)``."""

        cache_dir: Annotated[
            str,
            AttachOption(
                desc=(
                    "Directory for cached tsa.gov pages and parsed weekly reports (about 1 MB of Parquet per "
                    "week). Default: the per-user cache directory, e.g. ~/.cache/vgi-tsa or "
                    "~/Library/Caches/vgi-tsa."
                )
            ),
        ] = ""
        base_url: Annotated[
            str,
            AttachOption(
                desc=(
                    f"Where pages and PDFs are fetched from: {DEFAULT_BASE_URL} (default), a mirror URL, or a "
                    "local directory laid out like tsa.gov (travel/passenger-volumes/index.html, "
                    "travel/passenger-volumes/<year>/index.html, foia/readingroom/page-<n>.html, "
                    "sites/default/files/foia-readingroom/<report>.pdf)."
                )
            ),
        ] = DEFAULT_BASE_URL
        user_agent: Annotated[
            str,
            AttachOption(desc=f"User-Agent sent to tsa.gov (default {DEFAULT_USER_AGENT!r})."),
        ] = DEFAULT_USER_AGENT
        max_reports: Annotated[
            int,
            AttachOption(
                desc=(
                    "Most weekly report PDFs one checkpoint_throughput query may download and parse "
                    f"(default {DEFAULT_MAX_REPORTS}; each is ~5 MB and ~15 s). Cached weeks do not count."
                )
            ),
        ] = DEFAULT_MAX_REPORTS
        offline: Annotated[
            bool,
            AttachOption(desc="Never touch the network: use only what is cached, even if stale (default false)."),
        ] = False


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    TsaWorker.main()


if __name__ == "__main__":
    main()
