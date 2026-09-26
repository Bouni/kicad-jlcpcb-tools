"""Read JLCPCB's public rigid-board stackup catalogs without board uploads.

These are the JSON endpoints used by JLCPCB's website, not a documented
third-party integration API. Keep schema and identity checks here so a provider
change cannot silently select a different construction. Catalogs are returned
as immutable snapshots; this module never writes a cache or board configuration.
"""

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import socket
import ssl
import time
from typing import Any, Optional

import requests
from requests.auth import AuthBase
from urllib3 import exceptions as transport_errors
from urllib3.exceptions import HTTPError as TransportError
from urllib3.util import Timeout as RequestTimeout

from .stackup_model import Stackup, StackupLayer, validate_stackup

STACKUPS_URL = "https://jlcpcb.com/impedance"
CALCULATOR_URL = "https://jlcpcb.com/pcb-impedance-calculator"
API_ROOT = "https://jlcpcb.com/api"
ORDER_PATH = "/overseas-core-platform/shoppingCart/getImpedanceTemplateSettings"
CALCULATOR_PATH = "/jlcTools/impedance/selectPageImpedanceDefaultTemplate"
PREFERRED_SOURCE_URL = "https://jlcpcb.com/ssr/js/bb92ac0e1879b6977727.js"

_PAGE_SIZE = 100
_MAX_PAGES = 20
_MAX_RECORDS = _PAGE_SIZE * _MAX_PAGES
_MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_MAX_CATALOG_SECONDS = 120.0
Cancel = Optional[Callable[[], bool]]


class CatalogError(ValueError):
    """The complete provider catalog could not be safely retrieved or decoded."""


class CatalogCancelled(CatalogError):
    """The caller no longer needs the in-flight catalog or calculator request."""


class _NoOriginAuth(AuthBase):
    """Suppress implicit .netrc authentication without disabling network settings."""

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        """Keep public API requests anonymous; proxy authentication is separate."""
        request.headers.pop("Authorization", None)
        return request


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Inspect nested transport exception types without disclosing their text."""
    pending = [error]
    result: list[BaseException] = []
    seen: set[int] = set()
    while pending and len(result) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for child in (
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
            *current.args,
        ):
            if isinstance(child, BaseException):
                pending.append(child)
    return tuple(result)


def _transport_message(error: BaseException) -> str:
    """Explain the failure category without printing URLs, credentials, or paths."""
    causes = _exception_chain(error)
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in causes):
        detail = (
            "The TLS certificate for JLCPCB could not be verified. "
            "Check KiCad's Python CA certificates or your configured CA bundle. "
            "Certificate verification remains enabled."
        )
    elif any(
        isinstance(
            item,
            (
                requests.exceptions.ProxyError,
                requests.exceptions.InvalidProxyURL,
                transport_errors.ProxyError,
            ),
        )
        for item in causes
    ):
        detail = (
            "The configured proxy could not connect to JLCPCB. "
            "Check the system or environment proxy settings."
        )
    elif any(isinstance(item, socket.gaierror) for item in causes):
        detail = "JLCPCB's host name could not be resolved. Check DNS or the network connection."
    elif any(
        isinstance(
            item,
            (requests.exceptions.SSLError, transport_errors.SSLError, ssl.SSLError),
        )
        for item in causes
    ):
        detail = "The TLS connection to JLCPCB failed. Check the network and trusted certificate configuration."
    elif any(
        isinstance(
            item, (requests.exceptions.ReadTimeout, transport_errors.ReadTimeoutError)
        )
        for item in causes
    ):
        detail = "JLCPCB did not finish responding within the read timeout. Please try loading the catalog again."
    elif any(isinstance(item, transport_errors.NewConnectionError) for item in causes):
        # urllib3's NewConnectionError subclasses ConnectTimeoutError even for
        # a refused connection. Do not misreport every connection failure as a
        # timeout; DNS has already been identified from its nested cause above.
        detail = "The connection to JLCPCB could not be established. Check the network or proxy settings."
    elif any(
        isinstance(
            item,
            (requests.exceptions.ConnectTimeout, transport_errors.ConnectTimeoutError),
        )
        for item in causes
    ):
        detail = (
            "The connection to JLCPCB timed out. Check the network or proxy settings."
        )
    elif any(isinstance(item, requests.exceptions.InvalidSchema) for item in causes):
        detail = "The configured network transport or proxy is not supported by KiCad's Python installation."
    elif any(
        isinstance(
            item,
            (requests.exceptions.ChunkedEncodingError, transport_errors.ProtocolError),
        )
        for item in causes
    ):
        detail = "The connection ended before JLCPCB's response was complete. Please try loading the catalog again."
    elif isinstance(error, OSError):
        detail = "The network or configured TLS certificate bundle could not be opened. Check KiCad's network and certificate settings."
    else:
        detail = "Could not connect to JLCPCB's catalog/calculator service. Check the network or proxy settings."
    return f"{detail} The previous catalog and saved settings are unchanged."


def _check_cancel(cancel: Cancel, deadline: Optional[float] = None) -> None:
    """Check between bounded blocking reads, and before publishing any result."""
    if cancel is not None and cancel():
        raise CatalogCancelled("JLCPCB request cancelled.")
    if deadline is not None and time.monotonic() >= deadline:
        raise CatalogError(
            "JLCPCB request timed out. The previous catalog and settings are unchanged."
        )


def _response_chunks(
    response: requests.Response, cancel: Cancel, deadline: float
) -> Iterator[bytes]:
    """Check elapsed time between individual socket reads, not full buffers.

    Filling a large ``iter_content`` buffer permits a slow-drip response to keep
    it blocked indefinitely. urllib3's read1 performs one underlying read; the
    one-byte fallback retains the same bound with older urllib3 releases.
    """
    encoding = response.headers.get("Content-Encoding", "identity").casefold()
    if encoding not in ("", "identity"):
        raise CatalogError("JLCPCB ignored the uncompressed-response requirement.")
    read_one = getattr(response.raw, "read1", None)
    while True:
        _check_cancel(cancel, deadline)
        chunk = (
            read_one(64 * 1024, decode_content=False)
            if callable(read_one)
            else response.raw.read(1, decode_content=False)
        )
        _check_cancel(cancel, deadline)
        if not chunk:
            return
        yield chunk


def post_json(
    path: str,
    payload: dict[str, Any],
    *,
    cancel: Cancel = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Read a bounded JSON response from a fixed JLCPCB API path.

    Call only on a worker thread. No cookies, authentication, board files, or
    net names are supplied. Cancellation is checked between reads; an individual
    connect/read operation has its own short timeout. Redirects are not followed.
    """
    if (
        not isinstance(path, str)
        or not (path == ORDER_PATH or path.startswith("/jlcTools/impedance/"))
        or any(character in path for character in ("?", "#", "\\", ":"))
        or ".." in path
    ):
        raise CatalogError("Unsupported JLCPCB API path.")
    if isinstance(timeout, bool) or not 0 < timeout <= 120:
        raise CatalogError("Invalid JLCPCB request timeout.")
    deadline = time.monotonic() + timeout
    _check_cancel(cancel, deadline)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        # JLCPCB's calculator passes {langType: 'en'} to its HTTP wrapper;
        # that wrapper places this exact header on the wire. Without it the
        # backend returns Chinese labels whose classification can differ.
        "langType": "en",
        # Identify the native plugin without claiming a website origin/referrer.
        "User-Agent": "kicad-jlcpcb-tools/controlled-impedance",
    }
    try:
        with requests.Session() as session:
            # Keep configured proxies (including macOS system proxies) and
            # REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE. A truthy explicit auth handler
            # prevents Requests from looking up .netrc credentials separately.
            session.auth = _NoOriginAuth()
            with session.post(
                API_ROOT + path,
                json=payload,
                headers=headers,
                timeout=RequestTimeout(
                    total=timeout,
                    connect=min(10.0, timeout),
                    read=min(20.0, timeout),
                ),
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise CatalogError(
                        f"JLCPCB returned HTTP {response.status_code}. "
                        "The saved stackup has not changed."
                    )
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        announced_size = int(length)
                    except ValueError as exc:
                        raise CatalogError("Invalid JLCPCB response size.") from exc
                    if announced_size < 0 or announced_size > _MAX_RESPONSE_BYTES:
                        raise CatalogError("JLCPCB response exceeds the size limit.")
                content = bytearray()
                size = 0
                for chunk in _response_chunks(response, cancel, deadline):
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise CatalogError("JLCPCB response exceeds the size limit.")
                    content.extend(chunk)
    except (requests.RequestException, TransportError, OSError) as exc:
        _check_cancel(cancel)
        raise CatalogError(_transport_message(exc)) from exc
    _check_cancel(cancel, deadline)
    try:
        value = json.loads(
            content.decode("utf-8"),
            parse_constant=_invalid_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CatalogError("JLCPCB returned an invalid JSON response.") from exc
    if not isinstance(value, dict):
        raise CatalogError("JLCPCB returned an unexpected response object.")
    return value


def _invalid_json_constant(value: str) -> None:
    """Reject JavaScript NaN/Infinity extensions rather than accepting bad data."""
    raise CatalogError(f"JLCPCB returned a nonfinite JSON number: {value}.")


def _object(value: object, label: str) -> dict[str, Any]:
    """Require a provider object without accepting ambiguous coercions."""
    if not isinstance(value, dict):
        raise CatalogError(f"JLCPCB changed the {label} schema.")
    return value


def _text(value: object, label: str) -> str:
    """Require a bounded, nonempty provider label or stable identifier."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise CatalogError(f"JLCPCB returned an invalid {label}.")
    return value.strip()


def _number(value: object, label: str, *, allow_zero: bool = False) -> str:
    """Normalize source decimal dimensions without binary-float arithmetic."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, float, Decimal))
        or len(str(value)) > 64
    ):
        raise CatalogError(f"JLCPCB returned an invalid {label}.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise CatalogError(f"JLCPCB returned an invalid {label}.") from exc
    if not number.is_finite():
        raise CatalogError(f"JLCPCB returned an invalid {label}.")
    minimum_met = number >= 0 if allow_zero else number > 0
    if not minimum_met or number > 1_000_000 or abs(number.adjusted()) > 30:
        raise CatalogError(f"JLCPCB returned an invalid {label}.")
    formatted = format(number, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    """Require bounded JSON integers for counts and pagination."""
    if type(value) is not int or not minimum <= value <= _MAX_RECORDS:
        raise CatalogError(f"JLCPCB returned an invalid {label}.")
    return value


def _records(value: object, label: str) -> list[dict[str, Any]]:
    """Reject malformed or oversized record collections as a whole."""
    if not isinstance(value, list) or len(value) > _MAX_RECORDS:
        raise CatalogError(f"JLCPCB returned an invalid {label} collection.")
    return [_object(item, label) for item in value]


def _calculator_records(
    layer_count: int, cancel: Cancel, deadline: float
) -> list[dict[str, Any]]:
    """Read every advertised calculator page, rejecting drift and duplicates."""
    result: list[dict[str, Any]] = []
    total: Optional[int] = None
    seen_ids: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        _check_cancel(cancel, deadline)
        response = post_json(
            CALCULATOR_PATH,
            {
                "pageNum": page,
                "pageSize": _PAGE_SIZE,
                "plateLayerNumber": layer_count,
                "boardType": 1,
                "usePurpose": 1,
            },
            cancel=cancel,
            timeout=min(25.0, deadline - time.monotonic()),
        )
        if response.get("result") != "success" or response.get("success") is False:
            raise CatalogError("JLCPCB could not supply the calculator catalog.")
        body = _object(response.get("body"), "calculator catalog")
        current_total = _integer(body.get("total"), "catalog size")
        if total is not None and current_total != total:
            raise CatalogError(
                "JLCPCB's catalog changed while loading. Reopen the impedance dialog to check again."
            )
        total = current_total
        if _integer(body.get("pageNum"), "page number", minimum=1) != page:
            raise CatalogError("JLCPCB returned the wrong catalog page.")
        if _integer(body.get("pageSize"), "page size", minimum=1) != _PAGE_SIZE:
            raise CatalogError("JLCPCB changed its catalog pagination.")
        rows = _records(body.get("list"), "calculator stackup")
        expected = min(_PAGE_SIZE, max(total - len(result), 0))
        if len(rows) != expected:
            raise CatalogError("JLCPCB returned an incomplete calculator catalog.")
        for row in rows:
            key = _text(row.get("impedanceDefaultTemplateAccessId"), "calculator ID")
            if key in seen_ids:
                raise CatalogError("JLCPCB returned a duplicate calculator stackup.")
            seen_ids.add(key)
            result.append(row)
        if len(result) == total:
            return result
    raise CatalogError("JLCPCB's catalog exceeds the supported page limit.")


def is_preferred(reception_display_name: str) -> bool:
    """Mirror the website's fire-icon rule, independently of surcharge flags."""
    return "Standard" in reception_display_name or "推荐" in reception_display_name


def _unnamed(name: str) -> bool:
    """Exclude automatic/custom constructions exactly as the rigid calculator."""
    return (
        name.casefold() in ("no requirement", "custom stackup")
        or "无要求" in name
        or "自定义" in name
    )


def _charge_status(order: dict[str, Any], calculator: dict[str, Any]) -> str:
    """Keep provider pricing evidence separate from the preferred marker."""
    flag = calculator.get("chargeFlag")
    if type(flag) is bool:
        return "additional" if flag else "none"
    if flag is not None:
        raise CatalogError("JLCPCB changed the stackup surcharge flag format.")
    # Never turn a missing flag or absent fee into an assertion of free service.
    fees: list[Decimal] = []
    for key in ("fixedFee", "coefficient"):
        value = order.get(key)
        if value is not None:
            fees.append(Decimal(_number(value, "stackup fee", allow_zero=True)))
    if any(fee > 0 for fee in fees):
        return "additional"
    return "none" if len(fees) == 2 else "unknown"


def _calculator_layers(
    calculator: dict[str, Any], layer_count: int
) -> tuple[StackupLayer, ...]:
    """Expand cores with two copper faces into physical top-to-bottom layers."""
    result: list[StackupLayer] = []
    copper_names: list[str] = []
    for entry in _records(calculator.get("basicDataList"), "layer construction"):
        material_type = entry.get("materialType")
        material = _text(entry.get("materialName"), "layer material")
        if type(material_type) is not int:
            raise CatalogError("JLCPCB returned an invalid layer material type.")
        if material_type == 1:
            name = _text(entry.get("layerName"), "copper layer name")
            copper_names.append(name)
            result.append(
                StackupLayer(
                    name=name,
                    kind="copper",
                    thickness_mm=_number(
                        entry.get("topConductorThick"), "copper thickness"
                    ),
                    material=material,
                )
            )
        elif material_type == 3:
            # JLCPCB's frontend calls this "Bare board", assigns no copper
            # layer names, and treats top + dielectric + bottom thickness as
            # one unnamed physical dielectric. Some source rows retain nonzero
            # conductor-thickness metadata; those are not extra copper layers.
            # Match that explicit construction mapping, preserving its supplied
            # dielectric constant rather than inferring one from the label.
            if entry.get("layerName") not in (None, ""):
                raise CatalogError(
                    "JLCPCB assigned unexpected copper to a bare-board layer."
                )
            total_thickness = sum(
                (
                    Decimal(
                        _number(entry.get(key), "bare-board thickness", allow_zero=True)
                    )
                    for key in (
                        "topConductorThick",
                        "dielectricThick",
                        "botConductorThick",
                    )
                ),
                Decimal(0),
            )
            result.append(
                StackupLayer(
                    name="Bare board",
                    kind="dielectric",
                    thickness_mm=_number(total_thickness, "bare-board thickness"),
                    material=material,
                    dielectric_constant=_number(
                        entry.get("dielectricConstant"),
                        "bare-board dielectric constant",
                    ),
                )
            )
        elif material_type in (0, 2):
            dielectric = _number(entry.get("dielectricConstant"), "dielectric constant")
            thickness = _number(entry.get("dielectricThick"), "dielectric thickness")
            if material_type == 0:
                result.append(
                    StackupLayer(
                        name=f"Prepreg {len(result) + 1}",
                        kind="prepreg",
                        thickness_mm=thickness,
                        material=material,
                        dielectric_constant=dielectric,
                    )
                )
            else:
                names = _text(entry.get("layerName"), "core copper names").split("/")
                if len(names) != 2:
                    raise CatalogError("JLCPCB changed the core layer-name format.")
                copper_names.extend(names)
                result.extend(
                    (
                        StackupLayer(
                            name=names[0],
                            kind="copper",
                            thickness_mm=_number(
                                entry.get("topConductorThick"), "core copper thickness"
                            ),
                            material="Copper",
                        ),
                        StackupLayer(
                            name=f"Core {names[0]}/{names[1]}",
                            kind="core",
                            thickness_mm=thickness,
                            material=material,
                            dielectric_constant=dielectric,
                        ),
                        StackupLayer(
                            name=names[1],
                            kind="copper",
                            thickness_mm=_number(
                                entry.get("botConductorThick"), "core copper thickness"
                            ),
                            material="Copper",
                        ),
                    )
                )
        else:
            raise CatalogError("JLCPCB returned an unsupported rigid-board layer type.")
    if copper_names != [f"L{index}" for index in range(1, layer_count + 1)]:
        raise CatalogError("JLCPCB's physical copper layers do not match the board.")
    return tuple(result)


def _order_layers(order: dict[str, Any], layer_count: int) -> tuple[StackupLayer, ...]:
    """Preserve display-only ordering constructions absent from the calculator.

    Dielectric constants are deliberately left unknown. Such a stackup cannot
    be sent to the calculator and must be labelled unavailable by the caller.
    """
    result: list[StackupLayer] = []
    copper_count = 0
    rows = _records(order.get("iaminationList"), "ordering layer construction")
    for expected_sort, entry in enumerate(rows, 1):
        if type(entry.get("sort")) is not int or entry["sort"] != expected_sort:
            raise CatalogError("JLCPCB returned an unordered layer construction.")
        try:
            content = _object(
                json.loads(_text(entry.get("content"), "layer content")),
                "layer content",
            )
        except ValueError as exc:
            raise CatalogError(
                "JLCPCB returned invalid ordering layer content."
            ) from exc
        kind = entry.get("iaminationType")
        if type(kind) is not int:
            raise CatalogError("JLCPCB returned an invalid ordering material type.")
        if kind == 1:
            fields = (("LineThickness", "LineLayer", "lineMaterialType", "copper"),)
        elif kind == 2:
            fields = (("preThickness", "preLayer", "preMaterialType", "prepreg"),)
        elif kind == 3:
            fields = tuple(
                (
                    f"coreBoardThickness{index}",
                    f"coreBoardLayer{index}",
                    f"coreBoardMaterialType{index}",
                    "core" if index == 2 else "copper",
                )
                for index in range(1, 4)
            )
        else:
            raise CatalogError("JLCPCB returned an unsupported ordering layer type.")
        for thickness_key, name_key, material_key, normalized_kind in fields:
            thickness = _text(content.get(thickness_key), "ordering layer thickness")
            if not thickness.endswith("mm"):
                raise CatalogError("JLCPCB changed its ordering thickness units.")
            name = _text(content.get(name_key), "ordering layer name")
            if normalized_kind == "copper":
                copper_count += 1
                expected_name = (
                    "Top Layer"
                    if copper_count == 1
                    else "Bottom Layer"
                    if copper_count == layer_count
                    else f"Inner Layer L{copper_count}"
                )
                if name not in (expected_name, f"L{copper_count}"):
                    raise CatalogError(
                        "JLCPCB changed its physical copper layer order."
                    )
                name = f"L{copper_count}"
            result.append(
                StackupLayer(
                    name=name,
                    kind=normalized_kind,
                    thickness_mm=_number(thickness[:-2], "ordering layer thickness"),
                    material=_text(content.get(material_key), "ordering material"),
                )
            )
    if copper_count != layer_count:
        raise CatalogError("JLCPCB's ordering copper count does not match the board.")
    return tuple(result)


def _dimensions(
    record: dict[str, Any], *, calculator: bool
) -> tuple[int, str, str, str]:
    """Extract the explicit identity dimensions shared by both catalogs."""
    keys = (
        (
            "plateLayerNumber",
            "plateThickness",
            "cuprumThickness",
            "innerCopperThickness",
        )
        if calculator
        else ("stencilLayer", "stencilPly", "cuprumThickness", "insideCuprumThickness")
    )
    count = _integer(record.get(keys[0]), "copper layer count", minimum=2)
    return (
        count,
        _number(record.get(keys[1]), "board thickness"),
        _number(record.get(keys[2]), "outer copper weight"),
        # The calculator still supplies a placeholder inner weight for two-layer
        # boards, although there is no inner copper. Do not report it as real.
        "" if count == 2 else _number(record.get(keys[3]), "inner copper weight"),
    )


def fetch_stackups(
    layer_count: int,
    *,
    thickness_mm: Optional[str] = None,
    outer_copper_oz: Optional[str] = None,
    inner_copper_oz: Optional[str] = None,
    cancel: Cancel = None,
) -> tuple[Stackup, ...]:
    """Return all enabled, named rigid stackups for the PCB's full layer count.

    Optional thickness/copper filters are exact decimal matches. The two source
    catalogs are joined only by construction code and verified dimensions, never
    by display name. An orderable construction missing from the calculator stays
    visible with an empty calculator ID and unknown preferred status. Failures
    never publish a silently truncated catalog or mutate a saved selection.
    """
    if type(layer_count) is not int or not 2 <= layer_count <= 64 or layer_count % 2:
        raise CatalogError(
            "JLCPCB rigid stackups require an even copper-layer count from 2 to 64."
        )
    filters = tuple(
        None
        if value is None or (index == 2 and layer_count == 2)
        else _number(value, label)
        for index, (value, label) in enumerate(
            (
                (thickness_mm, "thickness filter"),
                (outer_copper_oz, "outer copper filter"),
                (inner_copper_oz, "inner copper filter"),
            )
        )
    )
    deadline = time.monotonic() + _MAX_CATALOG_SECONDS
    calculators = _calculator_records(layer_count, cancel, deadline)
    by_code: dict[str, dict[str, Any]] = {}
    for row in calculators:
        if _dimensions(row, calculator=True)[0] != layer_count:
            raise CatalogError("JLCPCB ignored the board's copper-layer filter.")
        if type(row.get("boardType")) is not int:
            raise CatalogError("JLCPCB returned an invalid board type.")
        if row["boardType"] != 1:
            continue
        display = _text(row.get("receptionDisplayName"), "calculator display name")
        if _unnamed(display):
            continue
        code = _text(row.get("laminatedConstructionCode"), "construction code")
        if code in by_code:
            raise CatalogError("JLCPCB returned an ambiguous construction code.")
        by_code[code] = row
    _check_cancel(cancel, deadline)
    response = post_json(
        ORDER_PATH,
        {"stencilLayer": layer_count},
        cancel=cancel,
        timeout=min(25.0, deadline - time.monotonic()),
    )
    if response.get("success") is not True or response.get("code") != 200:
        raise CatalogError("JLCPCB could not supply the ordering stackup catalog.")
    orders = _records(response.get("data"), "ordering stackup")
    fetched_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    result: list[Stackup] = []
    seen_order_codes: set[str] = set()
    for order in orders:
        _check_cancel(cancel, deadline)
        dimensions = _dimensions(order, calculator=False)
        if dimensions[0] != layer_count:
            raise CatalogError("JLCPCB ignored the board's copper-layer filter.")
        if (
            type(order.get("plateType")) is not int
            or type(order.get("enableFlag")) is not bool
        ):
            raise CatalogError("JLCPCB changed its ordering availability fields.")
        if order["plateType"] != 1 or not order["enableFlag"]:
            continue
        name = _text(order.get("showName"), "stackup name")
        if _unnamed(name):
            continue
        if any(
            wanted is not None and wanted != actual
            for wanted, actual in zip(filters, dimensions[1:])
        ):
            continue
        code = _text(order.get("impedanceTemplateCode"), "ordering construction code")
        if code in seen_order_codes:
            raise CatalogError("JLCPCB returned a duplicate ordering construction.")
        seen_order_codes.add(code)
        calculator = by_code.get(code, {})
        if calculator and _dimensions(calculator, calculator=True) != dimensions:
            raise CatalogError(
                "JLCPCB's ordering and calculator stackup dimensions disagree."
            )
        stackup = Stackup(
            stackup_id=code,
            name=name,
            layer_count=layer_count,
            thickness_mm=dimensions[1],
            outer_copper_oz=dimensions[2],
            inner_copper_oz=dimensions[3],
            preferred=is_preferred(calculator.get("receptionDisplayName", "")),
            charge_status=_charge_status(order, calculator),
            layers=(
                _calculator_layers(calculator, layer_count)
                if calculator
                else _order_layers(order, layer_count)
            ),
            calculator_id=calculator.get("impedanceDefaultTemplateAccessId", ""),
            source_url=STACKUPS_URL,
            retrieved_at_utc=fetched_at,
        )
        validate_stackup(stackup)
        result.append(stackup)
    _check_cancel(cancel, deadline)
    return tuple(
        sorted(
            result,
            key=lambda item: (
                not item.preferred,
                Decimal(item.thickness_mm),
                Decimal(item.outer_copper_oz),
                Decimal(item.inner_copper_oz or "0"),
                item.name.casefold(),
                item.stackup_id,
            ),
        )
    )
