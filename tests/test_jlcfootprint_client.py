"""Tests for the EasyEDA Pro client with the network faked: the batch lookup, documents, backoff."""

import json
import logging
from pathlib import Path

import pytest

from jlcfootprint.easyeda_client import (
    BACKOFF_S,
    BATCH_SIZE,
    DEVICES_URL,
    DOCUMENT_URL,
    FOOTPRINT,
    LCSC_COMPANY_PATH,
    SYMBOL,
    EasyEdaClient,
    retry_after_seconds,
)

from .jlcfootprint_support import recorded_devices, recorded_document

FIXTURES = Path(__file__).parent / "fixtures" / "jlcfootprint"
CLASSIC_UUID = "b3b82869fa924bae820e3a6cfb44d689"  # C2132's classic footprint uuid
SYMBOL_UUID = "c7fc7a92fb9f4171a873988b8332913e"  # C2132's symbol


class Response:
    """The three things the client reads from a response."""

    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    def json(self):
        """Return the body, or fail like requests does on a non-JSON page."""
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


class Network:
    """Serve queued responses and record every request, wait and token taken."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.waits = []
        self.tokens = 0
        self.stop_on_wait = False
        self.allow_tokens = True

    def _next(self):
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, headers=None, timeout=None):
        """Pop the next queued response for a GET."""
        self.calls.append(("GET", url, None))
        return self._next()

    def post(self, url, headers=None, json_body=None, timeout=None):
        """Pop the next queued response for a POST, keeping its body."""
        self.calls.append(("POST", url, json_body))
        return self._next()

    def wait(self, seconds):
        """Record the pause; a stop request interrupts it."""
        self.waits.append(seconds)
        return self.stop_on_wait

    def acquire(self):
        """Count the token; refuse when the worker is stopping."""
        self.tokens += 1
        return self.allow_tokens

    def client(self):
        """Build a client bound to this fake network."""
        return EasyEdaClient(
            get=self.get, post=self.post, wait=self.wait, acquire=self.acquire
        )


def classic_footprint_body():
    """Return the recorded classic-host per-uuid body: the dict form the Pro host also uses."""
    return json.loads(
        (FIXTURES / "easyeda_uuid" / f"uuid_{CLASSIC_UUID}.json").read_text()
    )


def test_search_by_codes_posts_one_chunk_and_classifies_every_code():
    """The codes and the library path go in the body; hits carry both uuids; the rest are misses."""
    codes, body = recorded_devices("misses")
    network = Network(Response(body=body))
    lookup = network.client().search_by_codes(codes)
    assert not lookup.failed and not lookup.transient
    assert lookup.requests == 1 and network.tokens == 1
    method, url, sent = network.calls[0]
    assert (method, url) == ("POST", DEVICES_URL)
    assert sent == {"codes": codes, "path": LCSC_COMPANY_PATH}
    assert len(lookup.devices.hits) == 25
    assert len(lookup.devices.missing) == len(codes) - 25
    hit = lookup.devices.hits["C2896143"]
    assert (hit.symbol_uuid, hit.puuid, hit.package_name) == (
        "2c7c51ed5bd845c7bc3cba2523aa337b",
        "6eefb1bcf6f14943ae6b87b3b923252b",
        "CAP-TH_L4.2-W3.8-P5.08-D0.6",
    )
    assert "C1369890" in lookup.devices.missing
    with pytest.raises(ValueError, match="at most 200"):
        network.client().search_by_codes([f"C{i}" for i in range(BATCH_SIZE + 1)])


def test_lookup_failures_are_transient_as_a_unit():
    """A refused, unreadable or unsuccessful answer classifies nothing and asks for a retry."""
    network = Network(Response(403), Response(429), Response(503), Response(403))
    lookup = network.client().search_by_codes(["C1", "C2"])
    assert lookup.failed and lookup.transient
    assert "after retries" in lookup.error
    assert network.waits == list(BACKOFF_S)
    assert lookup.devices.hits == {} and lookup.devices.missing == []

    network = Network(
        Response(body={"success": False, "code": 401, "message": "denied"})
    )
    lookup = network.client().search_by_codes(["C1"])
    assert lookup.failed and lookup.transient
    assert "401" in lookup.error

    network = Network(Response(body=None))
    lookup = network.client().search_by_codes(["C1"])
    assert lookup.failed and lookup.transient and "invalid JSON" in lookup.error


def test_fetch_footprint_and_symbol_parse_their_documents():
    """A footprint answer yields pads; a symbol answer yields numbered pins; one token each."""
    network = Network(Response(body=classic_footprint_body()))
    document = network.client().fetch_footprint(CLASSIC_UUID)
    assert (document.kind, document.uuid) == (FOOTPRINT, CLASSIC_UUID)
    assert document.record.status == "ok"
    assert [p["number"] for p in document.record.pads] == ["1", "2", "3"]
    assert document.record.package_name == "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR"
    assert not document.transient and document.requests == 1
    assert network.calls == [("GET", DOCUMENT_URL.format(uuid=CLASSIC_UUID), None)]

    network = Network(Response(body=recorded_document("symbol", SYMBOL_UUID)))
    document = network.client().fetch_symbol(SYMBOL_UUID)
    assert (document.kind, document.uuid) == (SYMBOL, SYMBOL_UUID)
    assert document.record.status == "ok"
    assert sorted((p.number, p.label) for p in document.record.pins) == [
        ("1", "B"),
        ("2", "E"),
        ("3", "C"),
    ]


def test_http_404_is_none_and_other_statuses_are_errors():
    """A missing document is final and empty; a 5xx is transient; an odd status is a final error."""
    document = Network(Response(404)).client().fetch_footprint("nope")
    assert (document.record.status, document.transient) == ("none", False)
    document = Network(Response(404)).client().fetch_symbol("nope")
    assert (document.record.status, document.transient) == ("none", False)
    network = Network(Response(500), Response(502), Response(504), Response(500))
    document = network.client().fetch_symbol("x")
    assert (document.record.status, document.transient) == ("error", True)
    document = Network(Response(418)).client().fetch_footprint("x")
    assert (document.record.status, document.transient) == ("error", False)
    assert "HTTP 418" in document.record.error
    not_found = {"success": False, "code": 404, "message": "Component not found"}
    document = Network(Response(body=not_found)).client().fetch_footprint("x")
    assert (document.record.status, document.transient) == ("none", False)


def test_a_document_refused_in_its_body_is_transient():
    """A success-false body without the not-found code is an error the breaker counts, like a refused lookup."""
    refused = {"success": False, "code": 429, "message": "Too many requests"}
    for fetch in ("fetch_footprint", "fetch_symbol"):
        document = getattr(Network(Response(body=refused)).client(), fetch)("x")
        assert (document.record.status, document.transient) == ("error", True)
        assert document.failed
        assert "429" in document.error
    found = Network(Response(404)).client().fetch_symbol("nope")
    assert not found.failed


def test_a_served_document_that_cannot_be_read_is_unreadable_not_transient():
    """Unreadable is EasyEDA's own answer; a failed request or a refusal is not one."""
    pinless = {
        "success": True,
        "code": 0,
        "result": {"title": "X", "dataStr": '["DOCTYPE","SYMBOL","1.1"]\n["HEAD",{}]'},
    }
    document = Network(Response(body=pinless)).client().fetch_symbol("sym-1")
    assert (document.record.status, document.transient) == ("error", False)
    assert document.unreadable and document.failed
    refused = {"success": False, "code": 429, "message": "Too many requests"}
    assert not Network(Response(body=refused)).client().fetch_symbol("x").unreadable
    assert not Network(Response(418)).client().fetch_symbol("x").unreadable
    assert not Network(Response(404)).client().fetch_symbol("x").unreadable


def test_rate_limit_backs_off_with_retry_after_then_the_ladder(caplog):
    """403 honours Retry-After, otherwise waits 60, 120, 240 s, then gives up transiently."""
    network = Network(
        Response(403, headers={"Retry-After": "30"}),
        Response(403),
        Response(body=recorded_document("symbol", SYMBOL_UUID)),
    )
    with caplog.at_level(logging.INFO, logger="jlcfootprint.easyeda_client"):
        document = network.client().fetch_symbol(SYMBOL_UUID)
    assert document.record.status == "ok"
    assert network.waits == [30.0, BACKOFF_S[1]]
    assert "HTTP 403 from EasyEDA; waiting 30 s" in caplog.text
    assert "waiting 120 s" in caplog.text
    assert document.requests == 3 and network.tokens == 3


def test_retry_after_parsing():
    """Only a positive number of seconds counts, capped to keep a session responsive."""
    assert retry_after_seconds({"Retry-After": "12"}) == 12.0
    assert retry_after_seconds({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) is None
    assert retry_after_seconds({"Retry-After": "0"}) is None
    assert retry_after_seconds({"Retry-After": "NaN"}) is None
    assert retry_after_seconds({"Retry-After": "inf"}) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds(None) is None
    assert retry_after_seconds({"Retry-After": "99999"}) == 600.0


@pytest.mark.parametrize(
    ("item", "transient", "fragment"),
    [
        (ConnectionError("reset"), True, "network error"),
        (Response(200, body=None), True, "invalid JSON"),
        (Response(200, body=["not", "an", "object"]), True, "not a JSON object"),
        (Response(200, body={"result": {}}), True, "no boolean success flag"),
        (Response(200, body={"success": "true"}), True, "no boolean success flag"),
        (Response(418), False, "HTTP 418"),
    ],
)
def test_failure_kinds(item, transient, fragment):
    """Network and body problems are transient; other HTTP statuses are final."""
    document = Network(item).client().fetch_footprint("x")
    assert document.record.status == "error"
    assert document.transient is transient
    assert fragment in document.record.error


def test_stopping_interrupts_a_backoff_and_a_token_wait():
    """Both interruption paths return a transient error without touching the network again."""
    network = Network(Response(403), Response(body=classic_footprint_body()))
    network.stop_on_wait = True
    document = network.client().fetch_footprint("x")
    assert document.record.status == "error"
    assert document.transient
    assert "stopped" in document.record.error
    assert len(network.calls) == 1

    network = Network(Response(body=classic_footprint_body()))
    network.allow_tokens = False
    lookup = network.client().search_by_codes(["C1"])
    assert "stopped before the request" in lookup.error and lookup.transient
    assert network.calls == []


def test_a_nan_retry_after_falls_back_to_the_backoff_step():
    """Retry-After: NaN is no delay: the client waits its own first step instead of raising."""
    network = Network(
        Response(429, headers={"Retry-After": "NaN"}),
        Response(body=classic_footprint_body()),
    )
    document = network.client().fetch_footprint(CLASSIC_UUID)
    assert document.record.status == "ok"
    assert network.waits == [BACKOFF_S[0]]
