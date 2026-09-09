"""Polite, defensive HTTP retrieval of the 速報 feed.

Design notes that are easy to get wrong and expensive to get wrong:

* **Conditional GET.**  NHK serves ``ETag``, ``Last-Modified`` and
  ``Cache-Control: max-age=60``.  Sending the stored validators turns the
  common case (nothing changed) into a 304 with no body, which is both cheaper
  for NHK and unambiguous for us.
* **No compression.**  The document is ~105 bytes empty and a few kB at its
  largest, so ``Accept-Encoding: gzip`` would buy nothing while adding a
  decompression path -- and a decompression bomb vector -- to the hot loop.
* **Bounded reads.**  The body is read with a hard cap, so a hostile or
  misrouted response cannot exhaust memory before validation ever runs.
* **Error bodies are not content.**  A 403 from an interposed WAF carries its
  own ``ETag`` and ``Last-Modified``; treating any 2xx-shaped thing as the feed
  is how archives silently fill with error pages.  Only 200 yields a body here.
"""

from __future__ import annotations

import dataclasses
import gzip
import http.client
import random
import socket
import time
import urllib.error
import urllib.request

__all__ = [
    "FetchResult",
    "FetchError",
    "Fetcher",
    "PRIMARY_URL",
    "MIRROR_URLS",
    "DEFAULT_USER_AGENT",
]

PRIMARY_URL = "https://api.web.nhk/sokuho/news/sokuho_news.xml"
# Same document, different edge hostname; used only when the primary fails.
MIRROR_URLS = ("https://news.web.nhk/sokuho/news/sokuho_news.xml",)

DEFAULT_USER_AGENT = (
    "sokuho-archive/1.0 (+https://github.com/Yanai-Taketo/sokuho-archive)"
)

# Read cap, generous against the real ~105 byte - few kB document.
MAX_BODY_BYTES = 1 << 20

_RETRYABLE_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    """The feed could not be retrieved. ``reason`` is a stable slug."""

    def __init__(self, reason: str, detail: str = "", *, status: int | None = None) -> None:
        self.reason = reason
        self.detail = detail
        self.status = status
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclasses.dataclass(frozen=True)
class FetchResult:
    """One completed retrieval attempt."""

    url: str
    status: int
    body: bytes | None
    etag: str | None
    last_modified: str | None
    fetched_at: str
    elapsed_ms: int
    attempts: int

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class Fetcher:
    """Retrieves the feed with retries, mirror fallback and conditional GET."""

    def __init__(
        self,
        url: str = PRIMARY_URL,
        *,
        mirrors: tuple[str, ...] = MIRROR_URLS,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        max_attempts: int = 4,
        backoff_base: float = 2.0,
        backoff_cap: float = 30.0,
        max_body_bytes: int = MAX_BODY_BYTES,
        sleeper=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.url = url
        self.mirrors = tuple(mirrors)
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_body_bytes = max_body_bytes
        self._sleep = sleeper
        self._rng = rng or random.Random()

    # -- internals ---------------------------------------------------------

    def _delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, so retries never synchronise."""
        ceiling = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        return self._rng.uniform(0.0, ceiling)

    def _request(self, url: str, etag: str | None, last_modified: str | None):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/xml, text/xml;q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return urllib.request.Request(url, headers=headers, method="GET")

    def _read_body(self, response) -> bytes:
        body = response.read(self.max_body_bytes + 1)
        if len(body) > self.max_body_bytes:
            raise FetchError(
                "body_too_large", f"response exceeded {self.max_body_bytes} bytes"
            )
        # Belt and braces: a misbehaving intermediary can compress despite the
        # identity request, and a silent mojibake snapshot is worse than a retry.
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError) as exc:
                # A truncated stream raises EOFError, which is not an OSError;
                # letting it escape would crash the poll loop instead of retrying.
                raise FetchError("bad_gzip", f"{type(exc).__name__}: {exc}") from exc
        return body

    # -- public API --------------------------------------------------------

    def fetch(
        self, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        """Fetch the feed, trying the primary URL then each mirror.

        Returns a :class:`FetchResult` for 200 (with ``body``) and 304 (without).
        Raises :class:`FetchError` when every URL and attempt is exhausted.
        """
        from .timeutil import utc_now_iso

        started = time.monotonic()
        attempts = 0
        last_error: FetchError | None = None

        for url in (self.url, *self.mirrors):
            for attempt in range(1, self.max_attempts + 1):
                attempts += 1
                try:
                    request = self._request(url, etag, last_modified)
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        status = response.status
                        body = self._read_body(response) if status == 200 else None
                        return FetchResult(
                            url=url,
                            status=status,
                            body=body,
                            etag=response.headers.get("ETag"),
                            last_modified=response.headers.get("Last-Modified"),
                            fetched_at=utc_now_iso(),
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            attempts=attempts,
                        )
                except urllib.error.HTTPError as exc:
                    # 304 arrives here, not as a normal response.
                    if exc.code == 304:
                        return FetchResult(
                            url=url,
                            status=304,
                            body=None,
                            etag=exc.headers.get("ETag") or etag,
                            last_modified=exc.headers.get("Last-Modified") or last_modified,
                            fetched_at=utc_now_iso(),
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            attempts=attempts,
                        )
                    exc.close()
                    last_error = FetchError("http_error", f"{exc.code} from {url}", status=exc.code)
                    if exc.code not in _RETRYABLE_STATUS:
                        break  # 404 and friends will not fix themselves; try the mirror.
                except FetchError as exc:
                    last_error = exc
                except (urllib.error.URLError, http.client.HTTPException, socket.timeout, OSError) as exc:
                    last_error = FetchError("network_error", f"{type(exc).__name__}: {exc}")

                if attempt < self.max_attempts:
                    self._sleep(self._delay(attempt))

        raise last_error or FetchError("exhausted", "no attempt produced a response")
