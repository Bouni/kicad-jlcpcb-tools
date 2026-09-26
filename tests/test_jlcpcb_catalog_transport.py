"""Catalog regressions derived from six-layer loading and connection failures.

These tests never contact JLCPCB or consult the user's proxy/netrc settings.
"""

from decimal import Decimal
import io
import socket
import ssl
from typing import Any, Optional

import pytest
import requests

from impedance.jlcpcb_stackups import (
    CALCULATOR_PATH,
    CatalogError,
    _calculator_layers,
    post_json,
)


@pytest.mark.parametrize(
    ("conductor_thickness", "expected_thickness"),
    [("0", "0.865"), ("0.0152", "0.8954")],
)
def test_bare_board_is_one_dielectric_without_phantom_copper(
    conductor_thickness: str, expected_thickness: str
) -> None:
    """Provider type 3 must not prevent a complete six-layer catalog loading."""

    def material(
        kind: int, name: Optional[str] = None, **overrides: Any
    ) -> dict[str, Any]:
        return {
            "materialType": kind,
            "materialName": "FR4" if kind != 1 else "Copper",
            "layerName": name,
            "dielectricThick": "0.1",
            "dielectricConstant": "4.55",
            "topConductorThick": "0.035",
            "botConductorThick": "0.035",
            **overrides,
        }

    layers = _calculator_layers(
        {
            "basicDataList": [
                material(1, "L1"),
                material(0),
                material(2, "L2/L3"),
                material(0),
                material(
                    3,
                    dielectricThick="0.865",
                    topConductorThick=conductor_thickness,
                    botConductorThick=conductor_thickness,
                ),
                material(0),
                material(2, "L4/L5"),
                material(0),
                material(1, "L6"),
            ]
        },
        6,
    )

    assert [layer.name for layer in layers if layer.kind == "copper"] == [
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
        "L6",
    ]
    bare = [layer for layer in layers if layer.kind == "dielectric"]
    assert len(bare) == 1
    assert Decimal(bare[0].thickness_mm) == Decimal(expected_thickness)
    assert bare[0].dielectric_constant == "4.55"
    assert bare[0].material == "FR4"


class _ResponseBody(io.BytesIO):
    """Support urllib3's single-read interface without a socket."""

    def read1(self, size: int = -1, decode_content: bool = False) -> bytes:
        return super().read1(size)


def test_transport_honors_proxy_and_ca_without_implicit_origin_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Requests' real prepare/merge path, replacing only socket send."""
    captured: dict[str, Any] = {}
    netrc_lookups: list[str] = []

    def netrc_auth(url: str, raise_errors: bool = False) -> tuple[str, str]:
        netrc_lookups.append(url)
        return ("must-not-send", "must-not-send")

    def environment_proxies(url: str, no_proxy: Any = None) -> dict[str, str]:
        return {"https": "http://configured-proxy.invalid:8080"}

    class Session(requests.Session):
        def send(
            self, request: requests.PreparedRequest, **kwargs: Any
        ) -> requests.Response:
            captured.update(kwargs)
            captured["headers"] = dict(request.headers)
            response = requests.Response()
            response.status_code = 200
            response.raw = _ResponseBody(b'{"success": true}')
            response.request = request
            return response

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", netrc_auth)
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", environment_proxies)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/configured/corporate-ca.pem")
    monkeypatch.setattr(requests, "Session", Session)

    assert post_json(CALCULATOR_PATH, {}) == {"success": True}
    assert captured["proxies"]["https"] == "http://configured-proxy.invalid:8080"
    assert captured["verify"] == "/configured/corporate-ca.pem"
    assert captured["allow_redirects"] is False
    assert "Authorization" not in captured["headers"]
    assert "Origin" not in captured["headers"]
    assert "Referer" not in captured["headers"]
    assert (
        captured["headers"]["User-Agent"] == "kicad-jlcpcb-tools/controlled-impedance"
    )
    assert netrc_lookups == []


@pytest.mark.parametrize(
    ("error", "expected_category"),
    [
        (
            requests.exceptions.SSLError(
                ssl.SSLCertVerificationError(1, "private-connection-detail")
            ),
            "certificate",
        ),
        (
            requests.exceptions.ProxyError("private-connection-detail"),
            "proxy",
        ),
        (
            requests.exceptions.ConnectionError(
                socket.gaierror(-2, "private-connection-detail")
            ),
            "dns",
        ),
        (
            requests.exceptions.ReadTimeout("private-connection-detail"),
            "read timeout",
        ),
    ],
)
def test_transport_errors_are_actionable_without_sensitive_details(
    monkeypatch: pytest.MonkeyPatch,
    error: requests.RequestException,
    expected_category: str,
) -> None:
    """The dialog should not reduce every transport failure to check connection."""

    def environment_proxies(url: str, no_proxy: Optional[str] = None) -> dict[str, str]:
        return {}

    class Session(requests.Session):
        def send(
            self, request: requests.PreparedRequest, **kwargs: Any
        ) -> requests.Response:
            raise error

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", environment_proxies)
    with pytest.raises(CatalogError) as failure:
        post_json(CALCULATOR_PATH, {})
    message = str(failure.value).lower()
    assert expected_category in message
    assert "private-connection-detail" not in message
    assert "unchanged" in message
