"""Tests for tools/jlcfootprint/fetch_easyeda_fixture.py with the network faked: pacing, backoff, what gets kept."""

import json
from pathlib import Path

import pytest
import requests

from tools.jlcfootprint import fetch_easyeda_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda"


class FakeResponse:
    """The three things the recorder reads from a response."""

    def __init__(self, status_code=200, body=None, text="<html>challenge</html>"):
        self.status_code = status_code
        self.body = body
        self.text = text

    def raise_for_status(self):
        """Mimic requests: an HTTPError for any 4xx or 5xx."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        """Return the body, or fail like requests does on a non-JSON page."""
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


@pytest.fixture
def fake_network(monkeypatch):
    """Serve queued responses, record every request URL, POST body and sleep."""
    recorder = fetch_easyeda_fixture
    log = {"urls": [], "sleeps": [], "queue": [], "posts": []}

    def get(url, headers=None, timeout=None):
        log["urls"].append(url)
        return log["queue"].pop(0)

    def post(url, headers=None, json=None, timeout=None):
        log["urls"].append(url)
        log["posts"].append(json)
        return log["queue"].pop(0)

    monkeypatch.setattr(recorder.requests, "get", get)
    monkeypatch.setattr(recorder.requests, "post", post)
    monkeypatch.setattr(recorder.time, "sleep", log["sleeps"].append)
    return recorder, log


def good_body():
    """Return a real recorded response, one the parser reads as ``ok``."""
    return json.loads((FIXTURES / "C2132.json").read_text(encoding="utf-8"))


def test_requests_are_paced_and_only_usable_bodies_are_kept(tmp_path, fake_network):
    """Interval sleeps sit between requests, never after the last; ok and none are written."""
    recorder, log = fake_network
    log["queue"] = [
        FakeResponse(body=good_body()),
        FakeResponse(body={"success": False, "code": 404, "message": "not found"}),
    ]
    assert (
        recorder.main(["C2132", "C99", "--out", str(tmp_path), "--interval", "10"]) == 0
    )
    assert len(log["urls"]) == 2 and log["urls"][0].endswith("/C2132/components")
    assert log["sleeps"] == [10.0]
    assert json.loads((tmp_path / "C2132.json").read_text())["success"] is True
    assert (tmp_path / "C99.json").exists()
    assert not list(tmp_path.glob("*.part"))


def test_failures_are_paced_too_and_three_in_a_row_stop_the_run(tmp_path, fake_network):
    """Non-retryable failures never burst, an error body is not kept, and the breaker trips."""
    recorder, log = fake_network
    log["queue"] = [
        FakeResponse(body=None),
        FakeResponse(body={"success": False, "code": 500, "message": "server"}),
        FakeResponse(status_code=404),
        FakeResponse(body=good_body()),
    ]
    rc = recorder.main(
        ["C1", "C2", "C3", "C4", "--out", str(tmp_path), "--interval", "10"]
    )
    assert rc == 1
    assert len(log["urls"]) == 3, "the fourth part must not be attempted"
    assert log["sleeps"] == [10.0, 10.0]
    assert not list(tmp_path.iterdir()), "no failed body is written"


def test_only_an_answer_or_a_genuine_none_is_kept(tmp_path, fake_network):
    """EasyEDA's not-found refusal and an empty answer are kept; a body without a boolean success flag is not."""
    recorder, log = fake_network
    log["queue"] = [
        FakeResponse(body={"success": False, "code": 404, "message": "not found"}),
        FakeResponse(body={"success": True, "result": None}),
        FakeResponse(body={"code": 500, "message": "busy"}),
        FakeResponse(body={"success": "false", "code": 500}),
    ]
    rc = recorder.main(
        ["C1", "C2", "C3", "C4", "--out", str(tmp_path), "--interval", "0"]
    )
    assert rc == 1
    assert len(log["urls"]) == 4
    assert sorted(path.name for path in tmp_path.iterdir()) == ["C1.json", "C2.json"]


def test_rate_limit_backs_off_then_gives_up_that_part(tmp_path, fake_network, capsys):
    """403 backs off 60, 120, 240 s (plus the interval) and the part is reported failed."""
    recorder, log = fake_network
    log["queue"] = [FakeResponse(status_code=403) for _ in range(4)] + [
        FakeResponse(body=good_body())
    ]
    rc = recorder.main(["C1", "C2132", "--out", str(tmp_path), "--interval", "10"])
    assert rc == 1
    assert log["sleeps"] == [60.0, 10.0, 120.0, 10.0, 240.0, 10.0, 10.0]
    assert not (tmp_path / "C1.json").exists()
    assert (tmp_path / "C2132.json").exists()
    assert "C1: FAILED" in capsys.readouterr().out


def test_existing_files_and_bad_codes_cost_no_request(tmp_path, fake_network):
    """A recorded part is skipped without a request; a malformed code stops before any."""
    recorder, log = fake_network
    (tmp_path / "C2132.json").write_text("{}", encoding="utf-8")
    assert recorder.main(["C2132", "--out", str(tmp_path)]) == 0
    assert log["urls"] == []
    with pytest.raises(SystemExit):
        recorder.main(["c2132", "--out", str(tmp_path)])
    assert log["urls"] == []


PRO_FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda_pro"
SYMBOL_UUID = "c7fc7a92fb9f4171a873988b8332913e"


def pro_symbol_body():
    """Return the recorded Pro symbol document for C2132."""
    return json.loads(
        (PRO_FIXTURES / f"symbol_{SYMBOL_UUID}.json").read_text(encoding="utf-8")
    )


def devices_fixture():
    """Return the codes and body of the recorded corner-case batch answer."""
    return json.loads(
        (PRO_FIXTURES / "devices_corner_case.json").read_text(encoding="utf-8")
    )


def test_batch_mode_posts_the_codes_and_keeps_them_with_the_answer(
    tmp_path, fake_network
):
    """One POST with the codes and the library path; the file holds both so misses are known."""
    recorder, log = fake_network
    devices = devices_fixture()
    log["queue"] = [FakeResponse(body=devices["body"])]
    rc = recorder.main(
        [
            "--batch",
            "C2132",
            "C7950",
            "--name",
            "two",
            "--out",
            str(tmp_path),
            "--interval",
            "1",
        ]
    )
    assert rc == 0
    assert log["urls"] == [recorder.DEVICES_URL]
    assert log["posts"] == [
        {"codes": ["C2132", "C7950"], "path": recorder.LCSC_COMPANY_PATH}
    ]
    saved = json.loads((tmp_path / "devices_two.json").read_text())
    assert saved["codes"] == ["C2132", "C7950"] and saved["body"] == devices["body"]
    with pytest.raises(SystemExit):
        recorder.main(["--batch", "C2132", "--out", str(tmp_path)])
    log["queue"] = [
        FakeResponse(body={"success": False, "code": 401, "message": "denied"})
    ]
    assert (
        recorder.main(["--batch", "C1", "--name", "bad", "--out", str(tmp_path)]) == 1
    )
    assert not (tmp_path / "devices_bad.json").exists()


def test_document_modes_record_footprints_and_symbols_from_the_pro_host(
    tmp_path, fake_network
):
    """--footprint and --symbol GET the Pro host; a symbol body without pads is kept as a symbol."""
    recorder, log = fake_network
    footprint = json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "jlcfootprint"
            / "easyeda_uuid"
            / "uuid_b3b82869fa924bae820e3a6cfb44d689.json"
        ).read_text()
    )
    log["queue"] = [FakeResponse(body=footprint), FakeResponse(body=pro_symbol_body())]
    rc = recorder.main(
        [
            "--footprint",
            "b3b82869fa924bae820e3a6cfb44d689",
            "--symbol",
            SYMBOL_UUID,
            "--out",
            str(tmp_path),
            "--interval",
            "1",
        ]
    )
    assert rc == 0
    assert log["urls"] == [
        recorder.PUUID_URLS["pro"].format(puuid="b3b82869fa924bae820e3a6cfb44d689"),
        recorder.PUUID_URLS["pro"].format(puuid=SYMBOL_UUID),
    ]
    assert log["sleeps"] == [1.0]
    assert (tmp_path / "footprint_b3b82869fa924bae820e3a6cfb44d689.json").exists()
    assert (tmp_path / f"symbol_{SYMBOL_UUID}.json").exists()
    # A footprint asked as a symbol is refused: its document holds no Pro-text symbol.
    log["queue"] = [FakeResponse(body=footprint)]
    assert (
        recorder.main(
            ["--symbol", "b3b82869fa924bae820e3a6cfb44d689", "--out", str(tmp_path)]
        )
        == 1
    )
    assert not (tmp_path / "symbol_b3b82869fa924bae820e3a6cfb44d689.json").exists()


def test_from_devices_queues_every_document_once(tmp_path, fake_network):
    """The footprints and symbols a batch answer names are queued, footprints first, no repeats."""
    recorder, log = fake_network
    devices = devices_fixture()
    (tmp_path / "devices_a.json").write_text(json.dumps(devices))
    footprints, symbols = recorder.uuids_from_devices([tmp_path / "devices_a.json"])
    assert len(footprints) == len(set(footprints)) and len(symbols) == len(set(symbols))
    assert 0 < len(footprints) <= len(devices["codes"])
    assert 0 < len(symbols) <= len(devices["codes"])
    for uuid in footprints + symbols:
        (tmp_path / f"footprint_{uuid}.json").write_text("{}")
        (tmp_path / f"symbol_{uuid}.json").write_text("{}")
    assert (
        recorder.main(
            ["--from-devices", str(tmp_path / "devices_a.json"), "--out", str(tmp_path)]
        )
        == 0
    )
    assert log["urls"] == [], "every document already exists"
