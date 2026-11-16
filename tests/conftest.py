"""Shared fixtures: a worker client and options reading the offline tsa.gov mirror."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from vgi.client import Client

from tsa_worker.common import Options, options_from

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "test" / "fixtures" / "tsa"
PDF_DIR = FIXTURES / "sites" / "default" / "files" / "foia-readingroom"


@pytest.fixture(scope="session")
def client() -> Iterator[Client]:
    """A client that launches the worker as a subprocess, the way DuckDB does."""
    with Client([sys.executable, "-m", "tsa_worker.worker"]) as c:
        yield c


@pytest.fixture
def opts(tmp_path: Path) -> Options:
    """Options reading the tsa.gov-shaped fixture tree into a fresh cache."""
    return options_from({"cache_dir": str(tmp_path / "cache"), "base_url": str(FIXTURES)})
