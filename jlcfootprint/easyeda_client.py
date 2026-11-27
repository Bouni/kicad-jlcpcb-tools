"""EasyEDA Pro client: the batch device lookup and the per-uuid documents.

Three calls, all on ``pro.easyeda.com``.  ``search_by_codes`` posts up to 200 LCSC
codes and gets each known part's symbol uuid, footprint uuid and package name;
``fetch_footprint`` and ``fetch_symbol`` get one document by its uuid.

HTTP 403, 429, 500, 502, 503 and 504 back off with ``Retry-After`` when present,
else 60, 120 and 240 s, and fail transiently after the last.  Network errors and
unreadable bodies are transient failures too, which a caller may ask again.  An
HTTP 404 is ``none`` (nothing there), another 5xx a transient error and any other
status a final one.  None of the three calls raises on a failed request;
``search_by_codes`` refuses more than 200 codes with ``ValueError``.

Pacing is the caller's: ``acquire`` is called before every request, retries
included, so the worker's buckets count each one; ``wait(seconds)`` returns True
to abandon a backoff when the caller is stopping.  ``requests``, which the plugin
already uses, is imported on first use; the rest is stdlib.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import math
import time
from typing import Any, Optional, Union

from .easyeda_parse import (
    DevicesResult,
    FootprintRecord,
    SymbolRecord,
    parse_devices_response,
    parse_puuid_response,
    parse_symbol_response,
)

logger = logging.getLogger(__name__)

DEVICES_URL = "https://pro.easyeda.com/api/devices/searchByCodes"
DOCUMENT_URL = "https://pro.easyeda.com/api/components/{uuid}"
# The LCSC library's path on the Pro host; the batch lookup searches inside it.
LCSC_COMPANY_PATH = "0819f05c4eef4c71ace90d822a990e87"
BATCH_SIZE = 200
FOOTPRINT = "footprint"
SYMBOL = "symbol"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
RETRYABLE_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
BACKOFF_S = (60.0, 120.0, 240.0)
MAX_RETRY_AFTER_S = 600.0
TIMEOUT_S = 20.0


class FetchError(Exception):
    """A request that yielded no usable body; ``transient`` says whether a retry may help."""

    def __init__(self, message: str, transient: bool, status: int = 0) -> None:
        super().__init__(message)
        self.transient = transient
        self.status = status


@dataclass
class Lookup:
    """One batch call's outcome: the devices found and the codes not found, or a failure."""

    codes: list[str]
    devices: DevicesResult = field(default_factory=DevicesResult)
    transient: bool = False  # True when the whole chunk should be asked again
    requests: int = 0

    @property
    def failed(self) -> bool:
        """Return True when the answer classified nothing."""
        return bool(self.devices.error)

    @property
    def error(self) -> str:
        """Return the failure text, empty on success."""
        return self.devices.error


@dataclass
class Document:
    """One per-uuid call's outcome: a footprint or a symbol record, and the failure kind."""

    kind: str  # 'footprint' | 'symbol'
    uuid: str
    record: Union[FootprintRecord, SymbolRecord]
    transient: bool = False
    requests: int = 0
    # EasyEDA answered with a document the parser could not read: the same
    # answer would come again, so it is no reason to ask again.
    unreadable: bool = False

    @property
    def failed(self) -> bool:
        """Return True when the document could not be had (its record is an ``error``)."""
        return self.record.status == "error"

    @property
    def error(self) -> str:
        """Return the failure text, empty on success."""
        return self.record.error


def _default_get(url: str, headers: dict, timeout: float) -> Any:
    """Perform the HTTP GET with requests, imported here so the package loads without it."""
    import requests  # noqa: PLC0415

    return requests.get(url, headers=headers, timeout=timeout)


def _default_post(url: str, headers: dict, json_body: Any, timeout: float) -> Any:
    """Perform the HTTP POST with a JSON body through requests."""
    import requests  # noqa: PLC0415

    return requests.post(url, headers=headers, json=json_body, timeout=timeout)


def _default_wait(seconds: float) -> bool:
    """Sleep; never interrupted."""
    time.sleep(seconds)
    return False


def retry_after_seconds(headers: Any) -> Optional[float]:
    """Return a numeric ``Retry-After`` header in seconds, capped, or None.

    None as well for a value that is not a positive finite number (``0``, ``-5``,
    ``NaN``), which the caller replaces with its own backoff step.
    """
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


class EasyEdaClient:
    """Fetch and parse Pro-host answers; test doubles replace ``get``, ``post``, ``wait``, ``acquire``."""

    def __init__(
        self,
        get: Optional[Callable[..., Any]] = None,
        post: Optional[Callable[..., Any]] = None,
        wait: Optional[Callable[[float], bool]] = None,
        acquire: Optional[Callable[[], bool]] = None,
        backoff: tuple[float, ...] = BACKOFF_S,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.get = get or _default_get
        self.post = post or _default_post
        # wait(seconds) returns True when the caller wants to stop instead of waiting on.
        self.wait = wait or _default_wait
        # acquire() blocks for the pacing token; False means stop without requesting.
        self.acquire = acquire or (lambda: True)
        self.backoff = tuple(backoff)
        self.timeout = timeout
        self.requests = 0

    def _request(self, url: str, json_body: Any = None) -> Any:
        """Return the decoded body, retrying rate limits and server errors with backoff.

        A POST when ``json_body`` is given, else a GET.  A body that is not JSON, or
        not EasyEDA's envelope (an object with a boolean ``success`` flag), is a
        transient failure like a network error: the part is asked again, and the
        worker's breaker counts it.  What the envelope carries is the parsers' to
        read.
        """
        for delay in (*self.backoff, None):
            if not self.acquire():
                raise FetchError("stopped before the request", transient=True)
            self.requests += 1
            try:
                if json_body is None:
                    response = self.get(url, headers=HEADERS, timeout=self.timeout)
                else:
                    response = self.post(
                        url, headers=HEADERS, json_body=json_body, timeout=self.timeout
                    )
            except (
                Exception
            ) as error:  # requests' own hierarchy; SSL and timeouts included
                raise FetchError(f"network error: {error}", transient=True) from error
            status = int(getattr(response, "status_code", 0))
            if status in RETRYABLE_STATUSES:
                if delay is None:
                    raise FetchError(
                        f"HTTP {status} after retries", transient=True, status=status
                    )
                pause = retry_after_seconds(getattr(response, "headers", None)) or delay
                logger.info(
                    "jlcfootprint: HTTP %d from EasyEDA; waiting %.0f s before trying again",
                    status,
                    pause,
                )
                if self.wait(pause):
                    raise FetchError(
                        f"HTTP {status}; stopped while backing off",
                        transient=True,
                        status=status,
                    )
                continue
            if status != 200:
                raise FetchError(
                    f"HTTP {status}", transient=status >= 500, status=status
                )
            try:
                body = response.json()
            except ValueError as error:
                raise FetchError(f"invalid JSON: {error}", transient=True) from error
            if not isinstance(body, dict):
                raise FetchError("response is not a JSON object", transient=True)
            if not isinstance(body.get("success"), bool):
                raise FetchError(
                    "response has no boolean success flag"
                    f" (success is {body.get('success')!r})",
                    transient=True,
                )
            return body
        raise AssertionError("unreachable: the last attempt returns or raises")

    def search_by_codes(self, codes: list[str]) -> Lookup:
        """Look up one chunk of at most 200 LCSC codes; a failed request never raises.

        A refusal or an unreadable answer fails the whole chunk transiently, so the
        caller may ask again, and so does a failed request unless its HTTP status
        was a final one; a well-formed answer classifies every code as a hit or a
        miss.
        """
        codes = list(codes)
        if len(codes) > BATCH_SIZE:
            raise ValueError(f"at most {BATCH_SIZE} codes per lookup, got {len(codes)}")
        before = self.requests
        try:
            body = self._request(
                DEVICES_URL, {"codes": codes, "path": LCSC_COMPANY_PATH}
            )
        except FetchError as error:
            return Lookup(
                codes,
                DevicesResult(error=str(error)),
                transient=error.transient,
                requests=self.requests - before,
            )
        devices = parse_devices_response(body, codes)
        return Lookup(
            codes,
            devices,
            transient=bool(devices.error),
            requests=self.requests - before,
        )

    def fetch_footprint(self, puuid: str) -> Document:
        """Fetch one footprint by uuid; never raises."""
        return self._fetch_document(
            FOOTPRINT, puuid, FootprintRecord, parse_puuid_response
        )

    def fetch_symbol(self, uuid: str) -> Document:
        """Fetch one symbol by uuid; never raises."""
        return self._fetch_document(SYMBOL, uuid, SymbolRecord, parse_symbol_response)

    def _fetch_document(
        self,
        kind: str,
        uuid: str,
        record_type: Callable[..., Union[FootprintRecord, SymbolRecord]],
        parse: Callable[[Any, str], Union[FootprintRecord, SymbolRecord]],
    ) -> Document:
        """Fetch one document by uuid and read it with ``parse``; never raises.

        A failed request becomes a ``record_type`` record carrying the error,
        ``none`` for HTTP 404.
        """
        before = self.requests
        try:
            body = self._request(DOCUMENT_URL.format(uuid=uuid))
        except FetchError as error:
            record = record_type(uuid, error=str(error))
            if error.status == 404:
                record.status = "none"
            return Document(
                kind,
                uuid,
                record,
                transient=error.transient,
                requests=self.requests - before,
            )
        record = parse(body, uuid)
        refused = _refused(body, record.status)
        return Document(
            kind,
            uuid,
            record,
            transient=refused,
            requests=self.requests - before,
            unreadable=record.status == "error" and not refused,
        )


def _refused(body: dict, status: str) -> bool:
    """Return True for a document EasyEDA refused in its body, not with a status.

    A ``success: false`` answer without EasyEDA's not-found code reads as an
    ``error``, asked again later.  It is a refusal like an HTTP 403 or 429, so it
    is transient and counts towards the worker's breaker, as a refused batch
    lookup does.
    """
    return body.get("success") is False and status == "error"
