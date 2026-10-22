"""Minimal polite HTTP fetcher (stdlib only) with an on-disk cache.

The harness core is deliberately stdlib + PyYAML, so scraping uses
:mod:`urllib.request` rather than pulling in ``requests``. Two things matter
when walking ~725 device pages:

* **caching** -- pages are ~1-3 MB; a content-addressed cache keyed by URL
  makes re-runs (and tests) free and lets a big crawl resume.
* **politeness** -- a minimum inter-request delay plus retry/backoff so we
  behave like a good citizen and survive transient 429/5xx.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import HilError

DEFAULT_USER_AGENT = (
    "hil-scraper/0.1 (+https://github.com/local/hil; stdlib urllib)"
)


class FetchError(HilError):
    """A URL could not be fetched after all retries."""


@dataclass
class FetchResult:
    url: str
    text: str
    status: int
    from_cache: bool


@dataclass
class Fetcher:
    """Cached, rate-limited, retrying GET.

    ``cache_dir`` may be ``None`` (no caching). ``min_delay`` is the minimum
    seconds between *network* requests (cache hits never sleep). Set
    ``offline=True`` to serve strictly from cache and never touch the network.
    """

    cache_dir: Path | None = None
    timeout: float = 40.0
    retries: int = 3
    min_delay: float = 0.4
    user_agent: str = DEFAULT_USER_AGENT
    offline: bool = False
    backoff: float = 2.0
    _last_request: float = field(default=0.0, init=False, repr=False)
    requests_made: int = field(default=0, init=False)
    cache_hits: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- cache ---------------------------------------------------------------

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest[:2]}/{digest}.html"

    def _cache_read(self, url: str) -> str | None:
        path = self._cache_path(url)
        if path is None or not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable cache file
            return None

    def _cache_write(self, url: str, text: str) -> None:
        path = self._cache_path(url)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:  # pragma: no cover - read-only cache dir
            pass

    # -- network -------------------------------------------------------------

    def _throttle(self) -> None:
        if self.min_delay <= 0:
            return
        wait = self.min_delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _download(self, url: str) -> tuple[str, int]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace"), resp.status

    def get(self, url: str) -> FetchResult:
        """Fetch ``url`` as text (cache first, then network with retries)."""
        cached = self._cache_read(url)
        if cached is not None:
            self.cache_hits += 1
            return FetchResult(url, cached, 200, from_cache=True)

        if self.offline:
            raise FetchError(
                f"offline mode and '{url}' is not cached. Re-run without "
                f"--offline (or with --cache-dir pointing at a populated cache)."
            )

        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                text, status = self._download(url)
                self.requests_made += 1
                self._cache_write(url, text)
                return FetchResult(url, text, status, from_cache=False)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                # 429 / 5xx are worth retrying with backoff; 4xx are not.
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
            if attempt < self.retries - 1:
                time.sleep(self.backoff ** attempt)

        raise FetchError(
            f"failed to fetch '{url}' after {self.retries} attempts: {last_exc}"
        )
