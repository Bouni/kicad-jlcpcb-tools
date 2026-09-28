"""Resolve shared schematic assignments from authoritative instance references."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Optional

import pytest

from .wx_harness import load_siblings


@pytest.fixture(params=[True, False], ids=["kicad7", "kicad8+"])
def render(request: pytest.FixtureRequest) -> Iterator[Callable[..., str]]:
    """Exercise the real span writer with a supplied authoritative PCB snapshot."""
    with load_siblings(
        "_schematic_instance_tests", ("schematic_fields",), {}
    ) as loaded:

        def write(
            text: str,
            assignments: Mapping[str, Optional[str]],
            *,
            project_name: Optional[Callable[[], Optional[str]]] = None,
            warnings: Optional[list[str]] = None,
        ) -> str:
            """Render against the validated assignment map without filesystem effects."""
            return loaded["schematic_fields"].update_assignment_fields(
                text,
                assignments,
                version7=request.param,
                project_name=project_name,
                warnings=warnings,
            )

        yield write


def _project(name: str, references: Sequence[str]) -> str:
    """Build direct project/path reference entries without variant overrides."""
    paths = "".join(
        f'        (path "/sheet-{index}" (reference "{reference}") (unit 1))\n'
        for index, reference in enumerate(references)
    )
    return f'      (project "{name}"\n{paths}      )\n'


def _symbol(reference: str, instances: str, *, fields: Optional[str] = None) -> str:
    """Make a placed symbol with an independently controlled displayed reference."""
    if fields is None:
        fields = '    (property "JLCPCB Part Number" "C100" (at 0 1 0))\n'
    return (
        '  (symbol (lib_id "Device:R")\n'
        f'    (property "Reference" "{reference}" (at 0 0 0))\n'
        + fields
        + instances
        + "  )\n"
    )


def _document(symbols: str) -> str:
    """Wrap placed symbols in a schematic with an unrelated library section."""
    return "(kicad_sch\n  (lib_symbols)\n" + symbols + ")\n"


def test_mixed_clear_and_assignment_preserve_reused_symbol(
    render: Callable[..., str],
) -> None:
    """One empty source cannot clear a field shared with an assigned instance."""
    instances = "    (instances\n" + _project("", ["R1", "R2"]) + "    )\n"
    original = _document(_symbol("R1", instances))

    assert render(original, {"R1": "", "R2": "C200"}) == original


def test_active_project_consensus_ignores_stale_displayed_reference(
    render: Callable[..., str],
) -> None:
    """Only active-project paths determine the shared base assignment."""
    instances = (
        "    (instances\n"
        + _project("board", ["R1", "R2"])
        + _project("foreign", ["R99"])
        + "    )\n"
    )
    original = _document(_symbol("R99", instances))

    written = render(
        original,
        {"R1": "C200", "R2": "C200", "R99": ""},
        project_name=lambda: "board",
    )

    assert written == original.replace('"C100"', '"C200"')


def test_comment_between_property_arguments_reuses_existing_alias(
    render: Callable[..., str],
) -> None:
    """Legal line comments cannot hide an existing assignment from the writer."""
    fields = (
        '    (property "JLCPCB Part Number"\n'
        '      # keep this comment and its "unclosed quote\n'
        '      "C100" (at 0 1 0))\n'
    )
    original = _document(_symbol("R1", "", fields=fields))

    assert render(original, {"R1": "C200"}) == original.replace('"C100"', '"C200"')


@pytest.mark.parametrize("value", ["C200", ""], ids=["assign", "proven-clear"])
def test_every_instance_must_agree_before_all_aliases_change(
    render: Callable[..., str], value: str
) -> None:
    """A complete consensus updates every alias while preserving unrelated text."""
    fields = (
        '    (property "JLCPCB Part Number" "C100" (at 0 1 0))\n'
        '    (property "JLC_PN" "C300" (at 0 2 0))\n'
        '    (property "Notes" "C100" (at 0 3 0))\n'
    )
    instances = "    (instances\n" + _project("", ["R1", "R2"]) + "    )\n"
    original = _document(_symbol("R99", instances, fields=fields))
    warnings: list[str] = []

    written = render(original, {"R1": value, "R2": value}, warnings=warnings)

    assert written == original.replace(
        '"JLCPCB Part Number" "C100"', f'"JLCPCB Part Number" "{value}"'
    ).replace('"JLC_PN" "C300"', f'"JLC_PN" "{value}"')
    assert warnings == []
    assert render(written, {"R1": value, "R2": value}) == written


@pytest.mark.parametrize(
    "assignments,reason",
    [
        ({"R1": "C200"}, "missing or unsafe"),
        ({"R1": ""}, "missing or unsafe"),
        ({"R1": "", "R2": None}, "missing or unsafe"),
        ({"R1": None, "R2": None}, "missing or unsafe"),
        ({"R1": "C200", "R2": "C300"}, "disagree"),
        ({"R1": "", "R2": "C200"}, "disagree"),
    ],
    ids=[
        "partial-assignment",
        "partial-clear",
        "unsafe-clear",
        "all-unsafe",
        "conflict",
        "mixed-clear",
    ],
)
def test_unsafe_instance_consensus_preserves_fields_and_reports_reason(
    render: Callable[..., str], assignments: dict[str, Optional[str]], reason: str
) -> None:
    """Missing and unsafe values cannot participate in an assignment or clear."""
    instances = "    (instances\n" + _project("", ["R1", "R2"]) + "    )\n"
    original = _document(_symbol("R1", instances))
    warnings: list[str] = []

    assert render(original, assignments, warnings=warnings) == original
    assert len(warnings) == 1
    assert "R1, R2" in warnings[0]
    assert reason in warnings[0]


@pytest.mark.parametrize(
    "groups,active_project",
    [
        ("", "board"),
        (_project("board", []), "board"),
        (_project("foreign", ["R1"]), "board"),
        (_project("board", ["R1"]), None),
        (_project("", ["R1"]) + _project("foreign", ["R2"]), "board"),
    ],
    ids=[
        "no-groups",
        "empty-active-group",
        "foreign-only",
        "unknown-project",
        "mixed-unnamed",
    ],
)
def test_present_unresolved_instances_never_fall_back_to_displayed_reference(
    render: Callable[..., str], groups: str, active_project: Optional[str]
) -> None:
    """A matching empty displayed-reference row is not authority for a clear."""
    original = _document(_symbol("R1", "    (instances\n" + groups + "    )\n"))
    warnings: list[str] = []

    assert (
        render(
            original,
            {"R1": "", "R2": "C200"},
            project_name=lambda: active_project,
            warnings=warnings,
        )
        == original
    )
    assert len(warnings) == 1
    assert "do not resolve" in warnings[0]


@pytest.mark.parametrize(
    "instances", ["", "    (instances\n" + _project("", ["R1"]) + "    )\n"]
)
def test_unscoped_symbol_does_not_request_project_identity(
    render: Callable[..., str], instances: str
) -> None:
    """Standalone and sole unnamed-group symbols remain usable without a project."""
    original = _document(_symbol("R1", instances))

    def unavailable_project() -> Optional[str]:
        """Fail if the writer asks for irrelevant project identity."""
        pytest.fail("Unscoped symbol requested project identity")

    assert render(
        original, {"R1": ""}, project_name=unavailable_project
    ) == original.replace('"C100"', '""')


def test_project_identity_is_resolved_once_and_symbols_remain_independent(
    render: Callable[..., str],
) -> None:
    """An unresolved first symbol cannot block a later standalone assignment."""
    scoped = "    (instances\n" + _project("board", ["R1", "R2"]) + "    )\n"
    other_scoped = "    (instances\n" + _project("board", ["R4"]) + "    )\n"
    first = _symbol("R1", scoped)
    second = _symbol("R3", "")
    third = _symbol("R4", other_scoped)
    calls: list[str] = []

    def project() -> Optional[str]:
        """Record requests for project identity during one pure render."""
        calls.append("project")
        return "board"

    written = render(
        _document(first + second + third),
        {"R1": "", "R2": "C200", "R3": "C300", "R4": ""},
        project_name=project,
    )

    assert written == _document(
        first + second.replace('"C100"', '"C300"') + third.replace('"C100"', '""')
    )
    assert calls == ["project"]


def test_nested_variant_and_foreign_references_cannot_join_base_consensus(
    render: Callable[..., str],
) -> None:
    """Only direct references inside active-project direct paths participate."""
    instances = """    (instances
      (project "board"
        (reference "R99")
        (path "/active" (reference "R1")
          (variant (name "Alternate")
            (reference "R99")
            (field (name "JLCPCB Part Number") (value "C900"))
            (path "/nested" (reference "R99"))
          )
        )
        (variant (path "/nested-project" (reference "R99")))
      )
      (project "foreign" (path "/foreign" (reference "R99")))
    )
"""
    original = _document(_symbol("R99", instances))

    written = render(original, {"R1": "", "R99": "C900"}, project_name=lambda: "board")

    assert written == original.replace('"C100"', '""')
    assert instances in written


def test_comments_and_native_escapes_are_supported_in_reference_arguments(
    render: Callable[..., str],
) -> None:
    """Reference and project arguments follow the same native lexical rules."""
    instances = """    (instances
      (project
        # active project
        "bo\\x61rd"
        (path "/active"
          (reference
            # placed reference
            "R\\x31")
        )
      )
    )
"""
    original = _document(_symbol("R99", instances))

    assert render(
        original, {"R1": "C200"}, project_name=lambda: "board"
    ) == original.replace('"C100"', '"C200"')


@pytest.mark.parametrize("assignments", [{}, {"R1": None}])
def test_unscoped_missing_or_unsafe_source_preserves_existing_assignment(
    render: Callable[..., str], assignments: dict[str, Optional[str]]
) -> None:
    """An absent or unsafe standalone assignment never means an explicit clear."""
    original = _document(_symbol("R1", ""))

    assert render(original, assignments) == original


def test_consensus_clear_does_not_insert_missing_alias(
    render: Callable[..., str],
) -> None:
    """Proven clears affect existing aliases without introducing new fields."""
    instances = "    (instances\n" + _project("", ["R1", "R2"]) + "    )\n"
    original = _document(_symbol("R1", instances, fields=""))

    assert render(original, {"R1": "", "R2": ""}) == original


@pytest.mark.parametrize(
    "reference_field",
    ["", '(reference "")', "(reference)", '(variant (reference "R2"))'],
    ids=["missing", "empty", "missing-value", "variant-only"],
)
def test_incomplete_active_path_prevents_clear_of_shared_assignment(
    render: Callable[..., str], reference_field: str
) -> None:
    """Every active instance must provide its own direct nonempty reference."""
    instances = (
        '    (instances (project "board"\n'
        '      (path "/first" (reference "R1"))\n'
        f'      (path "/unknown" {reference_field})\n'
        "    ))\n"
    )
    original = _document(_symbol("R1", instances))
    warnings: list[str] = []

    assert (
        render(original, {"R1": ""}, project_name=lambda: "board", warnings=warnings)
        == original
    )
    assert len(warnings) == 1


def test_incomplete_foreign_path_does_not_block_active_project_assignment(
    render: Callable[..., str],
) -> None:
    """A foreign project's missing reference does not join active authority."""
    instances = (
        "    (instances\n"
        + _project("board", ["R1"])
        + '      (project "foreign" (path "/unknown"))\n'
        + "    )\n"
    )
    original = _document(_symbol("R1", instances))
    warnings: list[str] = []

    assert render(
        original, {"R1": "C200"}, project_name=lambda: "board", warnings=warnings
    ) == original.replace('"C100"', '"C200"')
    assert warnings == []


@pytest.mark.parametrize(
    "instances",
    ["", "    (instances\n" + _project("", ["#PWR01", "#PWR02"]) + "    )\n"],
    ids=["standalone", "reused"],
)
def test_symbols_wholly_absent_from_pcb_preserve_silently(
    render: Callable[..., str], instances: str
) -> None:
    """Normal schematic-only power symbols do not produce export warnings."""
    original = _document(_symbol("#PWR01", instances))
    warnings: list[str] = []

    assert render(original, {"R1": "C200"}, warnings=warnings) == original
    assert warnings == []
