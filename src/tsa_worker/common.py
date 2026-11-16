"""Shared plumbing: ATTACH options, polite fetching from tsa.gov (or a local mirror) and the cache.

Nothing here touches the network at import or ATTACH time. Downloads happen only
when a query needs data that is not cached (or whose cache entry has expired).

A ``base_url`` may be a local directory laid out like tsa.gov. Pages map to files:

* ``travel/passenger-volumes``          -> ``travel/passenger-volumes/index.html``
* ``travel/passenger-volumes/2024``     -> ``travel/passenger-volumes/2024/index.html``
* ``foia/readingroom?...&page=3``       -> ``foia/readingroom/page-3.html``
* ``sites/default/files/.../x.pdf``     -> the same relative path
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import platformdirs
import truststore

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.tsa.gov"
DEFAULT_USER_AGENT = "vgi-tsa/0.1 (+https://query.farm)"
DEFAULT_MAX_REPORTS = 8
TIMEOUT_SECONDS = 120.0
MAX_ATTEMPTS = 4
LOCK_WAIT_SECONDS = 900.0
# Seconds between requests to tsa.gov when one query needs several pages.
POLITE_DELAY_SECONDS = 0.5


class UpstreamError(RuntimeError):
    """An upstream fetch failed in a way a retry will not fix."""


@dataclass(frozen=True, slots=True)
class Options:
    """The ATTACH options, with defaults filled in."""

    cache_dir: Path
    base_url: str
    user_agent: str
    offline: bool
    max_reports: int

    @property
    def is_remote(self) -> bool:
        """True when ``base_url`` is an http(s) URL rather than a local mirror directory."""
        return self.base_url.startswith(("http://", "https://"))

    @property
    def source_root(self) -> Path:
        """Cache directory for this ``base_url`` (a mirror never mixes with tsa.gov data)."""
        if self.base_url.rstrip("/") == DEFAULT_BASE_URL:
            return self.cache_dir / "tsa.gov"
        digest = hashlib.sha256(self.base_url.encode()).hexdigest()[:12]
        return self.cache_dir / f"mirror-{digest}"


def _value(raw: Any) -> Any:
    return raw.as_py() if hasattr(raw, "as_py") else raw


def options_from(attach_options: Mapping[str, Any]) -> Options:
    """Build `Options` from ``params.attach_options`` (missing names take their defaults)."""
    cache_dir = str(_value(attach_options.get("cache_dir")) or "").strip()
    base_url = str(_value(attach_options.get("base_url")) or "").strip() or DEFAULT_BASE_URL
    user_agent = str(_value(attach_options.get("user_agent")) or "").strip() or DEFAULT_USER_AGENT
    offline_raw = _value(attach_options.get("offline"))
    offline = offline_raw if isinstance(offline_raw, bool) else str(offline_raw).lower() in {"1", "true", "yes"}
    max_raw = _value(attach_options.get("max_reports"))
    try:
        max_reports = int(max_raw) if max_raw not in (None, "") else DEFAULT_MAX_REPORTS
    except (TypeError, ValueError):
        raise ValueError(f"max_reports must be an integer, got {max_raw!r}") from None
    if max_reports < 1:
        raise ValueError(f"max_reports must be at least 1, got {max_reports}")
    root = Path(cache_dir).expanduser() if cache_dir else Path(platformdirs.user_cache_dir("vgi-tsa"))
    if not base_url.startswith(("http://", "https://")):
        base_url = str(Path(base_url.removeprefix("file://")).expanduser().resolve())
    return Options(
        cache_dir=root.resolve(), base_url=base_url, user_agent=user_agent, offline=offline, max_reports=max_reports
    )


def mirror_path(rel: str) -> str:
    """Map a tsa.gov path (with optional query) to its file in a local mirror directory."""
    parsed = urllib.parse.urlsplit(rel.lstrip("/"))
    path = parsed.path.strip("/")
    page = urllib.parse.parse_qs(parsed.query).get("page")
    if page:
        return f"{path}/page-{int(page[0])}.html"
    if re.search(r"\.[A-Za-z0-9]{2,4}$", path):
        return path
    return f"{path}/index.html"


def source_url(opts: Options, rel: str) -> str:
    """Return the URL (or local file) a tsa.gov-relative path resolves to."""
    if opts.is_remote:
        return f"{opts.base_url.rstrip('/')}/{rel.lstrip('/')}"
    return str(Path(opts.base_url) / mirror_path(rel))


def public_url(rel: str) -> str:
    """Return the canonical tsa.gov URL of a relative path (for provenance columns)."""
    return f"{DEFAULT_BASE_URL}/{rel.lstrip('/')}"


_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    # The OS trust store: uv-managed and some packaged Pythons ship no usable CA bundle.
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return _SSL_CONTEXT


def _http_open(opts: Options, url: str) -> Any:
    headers = {"User-Agent": opts.user_agent, "Accept": "*/*"}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=_ssl_context())
        except urllib.error.HTTPError as err:
            if err.code == 403:
                raise UpstreamError(
                    f"GET {url} failed with HTTP 403: tsa.gov refused this client. The worker does not try to "
                    "get around bot protection; retry later, or set an identifying User-Agent with the "
                    "user_agent ATTACH option."
                ) from err
            if err.code == 404:
                raise FileNotFoundError(f"GET {url} returned HTTP 404") from err
            retryable = err.code == 429 or err.code >= 500
            if not retryable or attempt == MAX_ATTEMPTS:
                raise UpstreamError(f"GET {url} failed with HTTP {err.code}") from err
            retry_after = err.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * 2**attempt
            time.sleep(min(delay, 60.0))
        except urllib.error.URLError as err:
            if attempt == MAX_ATTEMPTS or isinstance(err.reason, ssl.SSLError):
                raise UpstreamError(f"GET {url} failed: {err.reason}") from err
            time.sleep(2.0 * 2**attempt)
    raise AssertionError("unreachable")


def fetch_text(opts: Options, rel: str) -> str:
    """Fetch an HTML page from tsa.gov or the mirror."""
    if opts.offline:
        raise UpstreamError(f"offline mode: not fetching {source_url(opts, rel)}")
    if not opts.is_remote:
        path = Path(source_url(opts, rel))
        if not path.exists():
            raise FileNotFoundError(f"{path} does not exist in the base_url mirror")
        return path.read_text(encoding="utf-8", errors="replace")
    log.info("fetching %s", source_url(opts, rel))
    with _http_open(opts, source_url(opts, rel)) as resp:
        return bytes(resp.read()).decode("utf-8", errors="replace")


def download(opts: Options, rel: str, target: Path) -> str:
    """Stream a file to ``target`` atomically and return its SHA-256."""
    if opts.offline:
        raise UpstreamError(f"offline mode: {source_url(opts, rel)} is not cached")
    url = source_url(opts, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.part")
    digest = hashlib.sha256()
    try:
        if opts.is_remote:
            log.info("downloading %s", url)
            with _http_open(opts, url) as resp, open(tmp, "wb") as out:
                while chunk := resp.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
        else:
            src = Path(url)
            if not src.exists():
                raise FileNotFoundError(f"{src} does not exist in the base_url mirror")
            with open(src, "rb") as inp, open(tmp, "wb") as out:
                while chunk := inp.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return digest.hexdigest()


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Cross-process lock (an O_EXCL lock file, treated as stale after LOCK_WAIT_SECONDS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                if time.time() - path.stat().st_mtime > LOCK_WAIT_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {path}") from None
            time.sleep(0.25)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    """Read a JSON cache file, or None when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Any) -> None:
    """Write a JSON cache file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)


def pylist(col: Any) -> list[Any]:
    """``col.to_pylist()`` typed as ``list[Any]``."""
    return list(col.to_pylist())
