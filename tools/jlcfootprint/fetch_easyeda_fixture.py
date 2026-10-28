"""Record EasyEDA responses as test fixtures (dev-time only; needs requests).

Usage:
    python3 tools/jlcfootprint/fetch_easyeda_fixture.py C2132 C1978115 [...] [--out DIR] [--interval 10]
    python3 tools/jlcfootprint/fetch_easyeda_fixture.py --puuid b3b82869fa924bae820e3a6cfb44d689 [--out DIR]
    python3 tools/jlcfootprint/fetch_easyeda_fixture.py --batch C2132 C7950 [...] --name corner_case [--interval 1]
    python3 tools/jlcfootprint/fetch_easyeda_fixture.py --footprint UUID [...] --symbol UUID [...] [--interval 1]
    python3 tools/jlcfootprint/fetch_easyeda_fixture.py --from-devices DEVICES.json [...] [--interval 1]

Classic per-LCSC responses land in ``tests/fixtures/jlcfootprint/easyeda/<lcsc>.json``;
classic per-uuid footprint responses in ``tests/fixtures/jlcfootprint/easyeda_uuid/
uuid_<uuid>.json``.  The EasyEDA Pro host's
answers, which the plugin fetches live, land in
``tests/fixtures/jlcfootprint/easyeda_pro/``: ``--batch`` records one
``searchByCodes`` answer as ``devices_<name>.json`` (the codes asked and the body,
so misses are known), ``--footprint`` and ``--symbol`` record one document each
as ``footprint_<uuid>.json`` and ``symbol_<uuid>.json``, and ``--from-devices``
queues every footprint and symbol a recorded batch answer names.  Existing files
are skipped, so re-running costs nothing.

Requests are spaced ``interval`` seconds apart whatever happened to the previous
one.  The classic host answers 403 after about 18 requests at 1.5 s, and 10 s is
safe there; the Pro host serves 2.5 requests per second without refusing, so
``--interval 1`` is fine for Pro-only runs.  A 403/429/5xx backs off 60, 120 and
240 s before that item is given up, and three items failing in a row stop the
run: a server that keeps refusing is left alone.  A response is kept only when
the package's parser reads it as ``ok`` (data for the item) or ``none`` (EasyEDA
has nothing for it); anything else is a failure and no file is written.  A kept
file is EasyEDA's answer as it came, written as compact JSON; a ``devices_*.json``
file wraps the answer with the codes it was asked for.  Exits 1 when any item
failed or was not attempted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Optional

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jlcfootprint.easyeda_parse import (  # noqa: E402
    parse_component_response,
    parse_devices_response,
    parse_puuid_response,
    parse_symbol_response,
)

URL = "https://easyeda.com/api/products/{lcsc}/components"
PUUID_URLS = {
    "classic": "https://easyeda.com/api/components/{puuid}",
    "pro": "https://pro.easyeda.com/api/components/{puuid}",
}
DEVICES_URL = "https://pro.easyeda.com/api/devices/searchByCodes"
LCSC_COMPANY_PATH = "0819f05c4eef4c71ace90d822a990e87"
BATCH_SIZE = 200
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
RETRYABLE = frozenset({403, 429, 500, 502, 503, 504})
BACKOFF_S = (60.0, 120.0, 240.0)
MAX_CONSECUTIVE_FAILURES = 3
LCSC_RE = re.compile(r"^C\d+$")
PUUID_RE = re.compile(r"^[0-9a-f]{32}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint"
DEFAULT_OUT = FIXTURES / "easyeda"
DEFAULT_PUUID_OUT = FIXTURES / "easyeda_uuid"
DEFAULT_PRO_OUT = FIXTURES / "easyeda_pro"


class Pacer:
    """Sleep ``interval`` seconds before every request but the first, whatever the last one did."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.requests = 0

    def __call__(self) -> None:
        """Wait out the interval, then count the request about to be made."""
        if self.requests:
            time.sleep(self.interval)
        self.requests += 1


def fetch(url: str, pace: Pacer, label: str, json_body: Any = None) -> Any:
    """Return the decoded response body for one URL, backing off on rate limits.

    A POST when ``json_body`` is given, else a GET.  Raises
    ``requests.RequestException`` (the last retryable status included) or
    ``ValueError`` (a body that is not JSON).  The parser classifies whatever comes back.
    """
    for delay in (*BACKOFF_S, None):
        pace()
        if json_body is None:
            response = requests.get(url, headers=HEADERS, timeout=20)
        else:
            response = requests.post(url, headers=HEADERS, json=json_body, timeout=20)
        if response.status_code in RETRYABLE and delay is not None:
            print(f"{label}: HTTP {response.status_code}, sleeping {delay:.0f}s")
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise AssertionError("unreachable: the last attempt returns or raises")


def write_fixture(path: Path, body: Any) -> None:
    """Write the body compactly through a partial file, so a crash leaves no half fixture."""
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    partial.replace(path)


def _accept(status: str, error: str) -> None:
    if status not in ("ok", "none"):
        raise ValueError(f"parser says {status!r}: {error or 'no detail'}")


def record(lcsc: str, out: Path, pace: Pacer) -> str:
    """Fetch one part from the classic host, keep the body when the parser accepts it."""
    body = fetch(URL.format(lcsc=lcsc), pace, lcsc)
    parsed = parse_component_response(body, lcsc)
    _accept(parsed.status, parsed.error)
    write_fixture(out / f"{lcsc}.json", body)
    return parsed.status


def record_puuid(puuid: str, out: Path, pace: Pacer) -> str:
    """Fetch one footprint by uuid from the classic host, keep it when the parser accepts it."""
    body = fetch(PUUID_URLS["classic"].format(puuid=puuid), pace, puuid)
    parsed = parse_puuid_response(body, puuid)
    _accept(parsed.status, parsed.error)
    write_fixture(out / f"uuid_{puuid}.json", body)
    return parsed.status


def record_batch(codes: list[str], name: str, out: Path, pace: Pacer) -> str:
    """Post one chunk of codes to the Pro batch lookup and keep the codes with the answer."""
    body = fetch(
        DEVICES_URL,
        pace,
        f"batch {name}",
        json_body={"codes": codes, "path": LCSC_COMPANY_PATH},
    )
    parsed = parse_devices_response(body, codes)
    if parsed.error:
        raise ValueError(f"parser says {parsed.error!r}")
    write_fixture(out / f"devices_{name}.json", {"codes": codes, "body": body})
    return f"{len(parsed.hits)} hit(s), {len(parsed.missing)} miss(es)"


def record_footprint(puuid: str, out: Path, pace: Pacer) -> str:
    """Fetch one footprint document from the Pro host, keep it when the parser accepts it."""
    body = fetch(PUUID_URLS["pro"].format(puuid=puuid), pace, puuid)
    parsed = parse_puuid_response(body, puuid)
    _accept(parsed.status, parsed.error)
    write_fixture(out / f"footprint_{puuid}.json", body)
    return parsed.status


def record_symbol(uuid: str, out: Path, pace: Pacer) -> str:
    """Fetch one symbol document from the Pro host, keep it when the parser accepts it."""
    body = fetch(PUUID_URLS["pro"].format(puuid=uuid), pace, uuid)
    parsed = parse_symbol_response(body, uuid)
    _accept(parsed.status, parsed.error)
    write_fixture(out / f"symbol_{uuid}.json", body)
    return parsed.status


def uuids_from_devices(paths: list[Path]) -> tuple[list[str], list[str]]:
    """Return the footprint uuids and the symbol uuids the recorded batch answers name."""
    footprints: list[str] = []
    symbols: list[str] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        codes = list(data["codes"])
        parsed = parse_devices_response(data["body"], codes)
        if parsed.error:
            raise SystemExit(f"{path}: {parsed.error}")
        for hit in parsed.hits.values():
            if hit.puuid and hit.puuid not in footprints:
                footprints.append(hit.puuid)
            if hit.symbol_uuid and hit.symbol_uuid not in symbols:
                symbols.append(hit.symbol_uuid)
    return footprints, symbols


def main(argv: Optional[list[str]] = None) -> int:
    """Record each requested item unless its fixture already exists."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("lcsc", nargs="*", help="LCSC part numbers, e.g. C2132")
    parser.add_argument(
        "--puuid",
        nargs="*",
        default=[],
        help="EasyEDA footprint uuids to record from the per-uuid endpoint",
    )
    parser.add_argument(
        "--batch",
        nargs="*",
        default=[],
        help="LCSC codes to look up in one Pro batch call (with --name)",
    )
    parser.add_argument("--name", help="fixture name for the --batch answer")
    parser.add_argument(
        "--footprint",
        nargs="*",
        default=[],
        help="footprint uuids to record from the Pro host as footprint_<uuid>.json",
    )
    parser.add_argument(
        "--symbol",
        nargs="*",
        default=[],
        help="symbol uuids to record from the Pro host as symbol_<uuid>.json",
    )
    parser.add_argument(
        "--from-devices",
        nargs="*",
        default=[],
        type=Path,
        help="recorded devices_*.json files whose footprints and symbols to record",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args(argv)
    pro_items = args.batch or args.footprint or args.symbol or args.from_devices
    if not args.lcsc and not args.puuid and not pro_items:
        parser.error(
            "nothing to record: give LCSC codes, --puuid, --batch, --footprint, --symbol or --from-devices"
        )
    malformed = [code for code in args.lcsc + args.batch if not LCSC_RE.match(code)]
    if malformed:
        parser.error(
            f"not LCSC codes (expected C followed by digits): {' '.join(malformed)}"
        )
    malformed = [
        uuid
        for uuid in args.puuid + args.footprint + args.symbol
        if not PUUID_RE.match(uuid)
    ]
    if malformed:
        parser.error(f"not uuids (32 hex digits): {' '.join(malformed)}")
    if args.batch and not (args.name and NAME_RE.match(args.name)):
        parser.error("--batch needs --name (letters, digits, - and _)")
    if len(args.batch) > BATCH_SIZE:
        parser.error(f"--batch takes at most {BATCH_SIZE} codes")
    items: list[tuple[str, Path, Any]] = [
        (code, (args.out or DEFAULT_OUT) / f"{code}.json", record) for code in args.lcsc
    ]
    items += [
        (uuid, (args.out or DEFAULT_PUUID_OUT) / f"uuid_{uuid}.json", record_puuid)
        for uuid in args.puuid
    ]
    pro_out = args.out or DEFAULT_PRO_OUT
    if args.batch:
        codes = list(args.batch)
        items.append(
            (
                f"batch {args.name}",
                pro_out / f"devices_{args.name}.json",
                lambda item, out, pace: record_batch(codes, args.name, out, pace),
            )
        )
    footprints = list(args.footprint)
    symbols = list(args.symbol)
    if args.from_devices:
        more_footprints, more_symbols = uuids_from_devices(args.from_devices)
        footprints += [uuid for uuid in more_footprints if uuid not in footprints]
        symbols += [uuid for uuid in more_symbols if uuid not in symbols]
    items += [
        (uuid, pro_out / f"footprint_{uuid}.json", record_footprint)
        for uuid in footprints
    ]
    items += [
        (uuid, pro_out / f"symbol_{uuid}.json", record_symbol) for uuid in symbols
    ]
    for _, path, _ in items:
        path.parent.mkdir(parents=True, exist_ok=True)
    pace = Pacer(args.interval)
    failed: list[str] = []
    not_attempted: list[str] = []
    consecutive = 0
    for index, (item, path, recorder) in enumerate(items):
        if path.exists():
            print(f"{item}: exists")
            continue
        try:
            print(f"{item}: {recorder(item, path.parent, pace)}")
            consecutive = 0
        except (requests.RequestException, ValueError) as error:
            print(f"{item}: FAILED ({error})")
            failed.append(item)
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                not_attempted = [
                    later
                    for later, later_path, _ in items[index + 1 :]
                    if not later_path.exists()
                ]
                print(
                    f"{consecutive} items failed in a row; stopping so EasyEDA is not hammered"
                )
                break
    if failed:
        print(f"failed: {' '.join(failed)} (re-run later; existing files are kept)")
    if not_attempted:
        print(f"not attempted: {' '.join(not_attempted)}")
    return 1 if failed or not_attempted else 0


if __name__ == "__main__":
    sys.exit(main())
