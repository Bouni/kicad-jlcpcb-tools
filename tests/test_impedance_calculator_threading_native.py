"""Verify calculator metadata handoff through real wx windows and GUI dispatch."""

from base64 import b64decode
from dataclasses import replace
import importlib
import os
from pathlib import Path
import sys
from threading import Event
import time
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .native_wx_support import isolated_native, pump, run_native, wait_until
from .test_impedance_calculator_metadata_validation import _configuration
from .test_impedance_draft_dialog import _board, _intent
from .test_impedance_spec_width_hint import _stackup

pytestmark = [
    pytest.mark.native_wx,
    pytest.mark.skipif(
        os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
        reason="native wx checks require an enabled desktop session",
    ),
]

_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("kind", ["draft", "automatic"])
def test_native_configuration_callbacks_and_closed_owner(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, kind: str
) -> None:
    """Real metadata fetches publish on GUI and remain safe after native destruction."""
    if isolated_native(request, "KICAD_IMPEDANCE_CALCULATOR_THREAD_CHILD"):
        return
    package_name = "_impedance_calculator_thread_native"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "impedance")]
    monkeypatch.setitem(sys.modules, package_name, package)
    api = SimpleNamespace(
        model=importlib.import_module(f"{package_name}.model"),
        dialog=importlib.import_module(f"{package_name}.dialog"),
    )
    calculator = importlib.import_module(f"{package_name}.jlcpcb_calculator")
    cache_module = importlib.import_module(f"{package_name}.calculator_config_cache")
    service = importlib.import_module(f"{package_name}.service")
    model = importlib.import_module(f"{package_name}.stackup_model")
    capture = service.CapturedImage.from_bytes(_PNG)
    finished = Event()
    writes: list[Any] = []
    violations: list[str] = []

    def exercise(frame: Any, wx: Any) -> None:
        def load() -> Any:
            if not wx.IsMainThread():
                violations.append("load")
            return cache_module.CalculatorConfigCache()

        def save(cache: Any) -> None:
            if not wx.IsMainThread():
                violations.append("save")
            writes.append(cache)

        def post(
            path: str, _payload: dict[str, object], **_kwargs: Any
        ) -> dict[str, Any]:
            if wx.IsMainThread():
                violations.append("HTTP on GUI")
            metadata = _configuration()
            records = {
                "selectPageImpedancePicture": ("body", metadata.models),
                "impedance-config/copper-trace-width/list": ("data", metadata.copper),
                "impedance-config/coverlay/list": ("data", metadata.coating),
                "selectImpedanceDefaultValue": ("data", metadata.limits),
            }
            key, values = records[path.removeprefix(calculator.API_PREFIX)]
            return {"code": 200, key: list(values)}

        class MetadataCalculator(calculator.JlcpcbCalculator):
            """Keep the complete metadata contract and substitute only the width solve."""

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(post=post, **kwargs)

            def calculate_width(
                self, stackup: Any, spec: Any, layer: str, *, cancel: Any
            ) -> Any:
                self._load_configuration(time.monotonic() + 5, cancel)
                finished.set()
                return model.WidthResult(
                    spec_id=spec.spec_id,
                    layer=layer,
                    input_digest=model.calculation_fingerprint(stackup, spec, layer),
                    status="success",
                    target_width_nm=150_000,
                    calculated_at_utc="2026-09-23T00:00:00.000000Z",
                    model="CoatedMicrostrip1B",
                    calculation_digest="a" * 64,
                )

        monkeypatch.setattr(calculator, "JlcpcbCalculator", MetadataCalculator)
        monkeypatch.setattr(
            api.dialog.ImpedanceDialog,
            "_ensure_catalog_started",
            lambda *_args: None,
        )
        board, stackup = _board(api), _stackup(api)
        config = replace(_intent(api), stackup=stackup)
        for close in (False, True):
            finished.clear()
            before = len(writes)
            kwargs = {
                "load_calculator_config": load,
                "save_calculator_config": save,
            }
            if kind == "draft":
                owner = api.dialog.SpecificationDialog(
                    frame,
                    board,
                    specification=config.specifications[0],
                    stackup=stackup,
                    **kwargs,
                )
                owner._draft_calc_timer.Stop()
                owner._on_draft_calc_timer(None)
            else:
                owner = api.dialog.ImpedanceDialog(
                    frame,
                    config,
                    board,
                    lambda *_args, **_kwargs: capture,
                    lambda: board,
                    **kwargs,
                )
                owner._schedule_width_refresh()
            assert finished.wait(timeout=5), "Metadata worker did not finish"
            assert len(writes) == before, "Metadata persisted before GUI dispatch"
            if close:
                if kind == "draft":
                    owner._cancel_draft_calculation()
                else:
                    owner._closing = True
                    owner._cancel_calculation()
                owner.Destroy()
            wait_until(wx, lambda before=before: len(writes) == before + 1)
            if close:
                wait_until(wx, lambda owner=owner: not owner)
            else:
                wait_until(
                    wx,
                    lambda owner=owner: bool(
                        owner.width_results
                        if kind == "draft"
                        else owner.session.config.width_results
                    ),
                )
                owner.Destroy()
            pump(wx)
        assert violations == []
        assert len(writes) == 2

    run_native(exercise)
