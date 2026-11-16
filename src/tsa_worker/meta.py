"""Discovery metadata for the tsa catalog.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Catalog-level tags live in ``CATALOG_TAGS``; every function and table
carries ``object_tags(...)``.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "tsa"

SOURCE_URL = "https://github.com/Query-farm/vgi-tsa"
VOLUMES_URL = "https://www.tsa.gov/travel/passenger-volumes"
READING_ROOM_URL = "https://www.tsa.gov/foia/readingroom"

SCHEMA_CATEGORIES = [
    {
        "name": "volumes",
        "title": "National Volumes",
        "description": "Nationwide daily counts of travelers screened at TSA checkpoints.",
    },
    {
        "name": "checkpoints",
        "title": "Checkpoint Throughput",
        "description": "Hourly passenger counts per airport checkpoint from TSA's weekly FOIA reports.",
    },
    {"name": "cache", "title": "Cache", "description": "Inspect the local download and parse cache."},
]


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


_DUCK_TYPES = {
    pa.string(): "VARCHAR",
    pa.int32(): "INTEGER",
    pa.int64(): "BIGINT",
    pa.float64(): "DOUBLE",
    pa.bool_(): "BOOLEAN",
    pa.date32(): "DATE",
    pa.timestamp("us", tz="UTC"): "TIMESTAMP WITH TIME ZONE",
}


def result_columns(schema: pa.Schema) -> str:
    """``vgi.result_columns_schema`` JSON from an Arrow schema built with `column`."""
    return json.dumps(
        [
            {
                "name": f.name,
                "type": _DUCK_TYPES[f.type],
                "description": (f.metadata or {}).get(b"comment", b"").decode(),
            }
            for f in schema
        ]
    )


LATENCY_NOTE = (
    "Nothing here is real time: the national daily count is posted with a lag of about one to three "
    "days, and the hourly checkpoint reports arrive as weekly PDFs one to two weeks after the week ends."
)

CATALOG_DOC_LLM = (
    "Transportation Security Administration (TSA) checkpoint passenger data, read from tsa.gov on demand "
    "and cached locally. Two sources: (1) TSA's nationwide count of travelers screened per day since "
    "2019-01-01, and (2) the weekly 'TSA Throughput' reports TSA posts in its FOIA Electronic Reading Room "
    "(PDFs, January 2017 onward with a few gaps), giving passengers screened per hour at each "
    "checkpoint of about 420 airports. "
    + LATENCY_NOTE
    + " Airport codes are 3-letter IATA codes (join to airport reference data on the IATA code); states "
    "are 2-letter USPS codes; hours are local airport time as printed by TSA. The MyTSA wait-time web "
    "service that once served live checkpoint waits is retired, so no wait-time data is offered."
)

CATALOG_DOC_MD = (
    "# TSA checkpoint throughput\n\n"
    "Passenger volumes screened at Transportation Security Administration checkpoints, from "
    f"[tsa.gov]({VOLUMES_URL}):\n\n"
    "- **National daily counts** since 2019, from TSA's passenger-volumes pages.\n"
    "- **Hourly counts per airport checkpoint**, parsed from the weekly throughput PDFs in TSA's "
    f"[FOIA reading room]({READING_ROOM_URL}).\n\n"
    f"{LATENCY_NOTE}\n\n"
    "Data is fetched when a query needs it and cached on disk. TSA publications are U.S. government "
    "works and in the public domain."
)

CATALOG_COMMENT = (
    "TSA checkpoint passenger volumes in DuckDB: the national daily traveler count since 2019 and hourly "
    "throughput per airport checkpoint parsed from TSA's weekly FOIA reports. Published with a lag of "
    "days to weeks; not real time."
)

SCHEMA_COMMENT = (
    "TSA passenger screening volumes: the nationwide daily count and hourly per-checkpoint counts from the "
    "weekly FOIA throughput reports, plus the report listing and the local cache."
)

SCHEMA_DOC_LLM = (
    "TSA passenger screening volumes at two grains. The national grain is one count per day of everyone "
    "screened at TSA checkpoints, back to 2019; it is cheap to read (one HTML page per year) and is the "
    "right source for nationwide trends, holidays and year-over-year comparisons. The checkpoint grain is "
    "passengers per hour per airport checkpoint, parsed from TSA's weekly FOIA PDF reports (2017 onward, "
    "about 420 airports); each week costs a ~5 MB download and ~15 s of parsing on first use, and the "
    "max_reports ATTACH option (default 8) caps how many uncached weeks one query may fetch, so ask for "
    "short date ranges and check which weeks exist before asking for recent ones. Airports are IATA "
    "codes, states USPS codes, hours local airport time. Rows the PDF parser cannot validate are counted "
    "and inspectable, never silently dropped. " + LATENCY_NOTE
)

SCHEMA_DOC_MD = (
    "## main\n\n"
    "- **National volumes**: one row per day since 2019-01-01.\n"
    "- **Checkpoint throughput**: hourly passengers per airport checkpoint, one weekly report at a time.\n"
    "- **Cache**: what has been downloaded and parsed.\n\n" + LATENCY_NOTE
)

AGENT_TEST_TASKS = [
    {
        "name": "busiest_day_2024",
        "prompt": "Using the tsa worker, on which date in 2024 did TSA screen the most travelers nationwide, "
        "and how many?",
    },
    {
        "name": "latest_report_week",
        "prompt": "What is the most recent week covered by TSA's hourly airport checkpoint throughput "
        "reports? Give its first and last day.",
    },
    {
        "name": "atl_checkpoints",
        "prompt": "In the TSA hourly throughput report for November 1-7, 2026, how many passengers were "
        "screened at each checkpoint of Atlanta's airport (ATL) over the whole week?",
    },
    {
        "name": "national_2019_vs_2025",
        "prompt": "Compare total travelers screened by TSA nationwide in 2019 and in 2025.",
    },
    {
        "name": "report_parse_quality",
        "prompt": "For TSA's hourly checkpoint report covering November 1-7, 2026, how many table rows "
        "failed the parser's validation, and how many weekly reports are now parsed in the local cache?",
    },
]

CATALOG_TAGS = {
    "vgi.title": "TSA Checkpoint Throughput",
    "vgi.subtitle": "TSA passenger screening volumes: national daily counts and hourly counts per airport checkpoint.",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.keywords": keywords_array(
        "tsa, airport, checkpoint, passenger volumes, throughput, air travel, security screening, foia"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    # Public analyst tasks for `vgi-lint simulate`; private graders live in vgi-agent-tests.yaml.
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
