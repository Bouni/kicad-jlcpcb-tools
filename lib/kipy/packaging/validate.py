# Copyright The KiCad Developers
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from kipy.packaging.types import ValidationMessage, ValidationReport

_PLUGIN_SCHEMA = "api.v1.schema.json"
_PCM_SCHEMAS = ("pcm.v2.schema.json", "pcm.v1.schema.json")


def validate_plugin(source_dir: str | Path) -> ValidationReport:
    root = Path(source_dir).expanduser().resolve()
    report = ValidationReport(root=root)

    if not root.exists():
        report.add_error("plugin directory does not exist")
        return report

    if not root.is_dir():
        report.add_error("plugin path is not a directory")
        return report

    plugin_path = root / "plugin.json"

    if not plugin_path.exists():
        report.add_error(
            "plugin.json does not exist. This tool can only validate IPC API plugins."
        )
        return report

    plugin = _load_plugin_json(plugin_path, report)
    _validate_requirements(root, report)

    if plugin is None:
        return report

    _validate_plugin_schema(plugin, report)
    _validate_plugin_data(root, plugin, report)
    return report


def validate(path: str | Path) -> ValidationReport:
    candidate = Path(path).expanduser().resolve()
    report = ValidationReport(root=candidate)

    if candidate.suffix.lower() == ".zip":
        report.add_info("Validating PCM package archive...")

        if not candidate.exists():
            report.add_error("PCM package archive does not exist")
            return report

        if not candidate.is_file():
            report.add_error("PCM package path is not a file")
            return report

        with tempfile.TemporaryDirectory(prefix="kicad_packaging_validation_") as tmp_dir:
            extract_root = Path(tmp_dir)

            try:
                with zipfile.ZipFile(candidate) as archive:
                    for item in archive.infolist():
                        zip_path = Path(item.filename)
                        if zip_path.is_absolute() or ".." in zip_path.parts:
                            report.add_error(
                                "archive contains an unsafe path",
                                path=item.filename,
                            )
                            return report

                    archive.extractall(extract_root)
            except zipfile.BadZipFile as ex:
                report.add_error(f"invalid zip archive: {ex!s}")
                return report
            except OSError as ex:
                report.add_error(f"failed to extract zip archive: {ex!s}")
                return report

            _validate_pcm_package_data(extract_root, report)
            return report

    if candidate.is_dir() and (candidate / "metadata.json").exists():
        report.add_info("Found metadata.json; validating as PCM package")
        _validate_pcm_package_data(candidate, report)
        return report

    return validate_plugin(candidate)


def _load_plugin_json(
    plugin_path: Path,
    report: ValidationReport,
) -> dict[str, Any] | None:
    if not plugin_path.exists():
        report.add_error("missing required file: plugin.json", path="plugin.json")
        return None

    try:
        data = json.loads(plugin_path.read_text(encoding="utf-8"))
    except OSError as ex:
        report.add_error(f"failed to read plugin.json: {ex!s}", path="plugin.json")
        return None
    except json.JSONDecodeError as ex:
        report.add_error(f"invalid JSON: {ex!s}", path="plugin.json")
        return None

    if not isinstance(data, dict):
        report.add_error("plugin.json root must be a JSON object", path="plugin.json")
        return None

    return data


def _validate_requirements(root: Path, report: ValidationReport):
    requirements_path = root / "requirements.txt"

    if not requirements_path.exists():
        report.add_warning(
            "missing requirements.txt, KiCad will not install any Python packages for your plugin",
            path="requirements.txt",
        )
        return

    try:
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except OSError as ex:
        report.add_error(
            f"failed to read requirements.txt: {ex!s}",
            path="requirements.txt",
        )
        return

    dependencies = [_parse_requirement_name(line) for line in lines]
    dependency_names = {name for name in dependencies if name is not None}

    if "kicad-python" not in dependency_names:
        report.add_warning(
            "requirements.txt does not include kicad-python",
            path="requirements.txt",
        )


def _validate_plugin_schema(plugin: dict[str, Any], report: ValidationReport):
    if "$schema" not in plugin:
        report.add_error("plugin.json must include a $schema reference")
    elif "https://go.kicad.org/api/schemas/v" not in plugin["$schema"]:
        report.add_error(f"unexpected schema URI: {plugin['$schema']}")

    try:
        schema_text = (
            files("kipy.packaging.schemas").joinpath(_PLUGIN_SCHEMA).read_text(encoding="utf-8")
        )
        schema = json.loads(schema_text)
    except ValueError as e:
        report.add_error(
            f"failed to read plugin schema file: {e!s}",
            path=f"kipy.packaging.schemas:{_PLUGIN_SCHEMA}",
        )
        return

    validator = Draft7Validator(schema)

    for error in sorted(validator.iter_errors(plugin), key=lambda current: list(current.path)):
        report.add_error(
            f"schema validation error: {error.message}",
            path=f"plugin.json{_format_schema_error_path(error.path)}",
        )


def _validate_plugin_data(root: Path, plugin: dict[str, Any], report: ValidationReport):
    actions = plugin.get("actions")
    if not isinstance(actions, list):
        # Type checker assertion; should be enforced by schema
        return

    if len(actions) == 0:
        report.add_error(
            "plugin must define at least one action",
            path="plugin.json:actions",
        )
        return

    for action_index, action in enumerate(actions):
        if not isinstance(action, dict):
            # Type checker assertion; should be enforced by schema
            continue

        prefix = f"plugin.json:actions[{action_index}]"
        entrypoint = action.get("entrypoint")

        if isinstance(entrypoint, str):
            _validate_file_path(
                root,
                entrypoint,
                report,
                f"{prefix}:entrypoint",
                "entrypoint file",
            )

        for key in ("icons-light", "icons-dark"):
            icons = action.get(key)
            if not isinstance(icons, list):
                # Not an error; optional
                continue

            for icon_index, icon in enumerate(icons):
                if not isinstance(icon, str) or icon.strip() == "":
                    continue

                _validate_file_path(
                    root,
                    icon,
                    report,
                    f"{prefix}:{key}[{icon_index}]",
                    "icon file",
                )


def _validate_pcm_package_data(root: Path, report: ValidationReport):
    metadata = _load_metadata_json(root, report)
    if metadata is None:
        return

    if not _validate_pcm_schema(metadata, report):
        return

    if "versions" in metadata:
        for version in metadata["versions"]:
            if "download_sha256" in version or "download_url" in version:
                report.add_error(
                    "metadata.json inside the archive must not contain download_* keys",
                    path="metadata.json",
                )

    if metadata["type"] != "plugin":
        report.add_info("package is not a plugin; no further validation to do")
        return

    plugins_dir = root / "plugins"
    if not plugins_dir.exists() or not plugins_dir.is_dir():
        report.add_error("PCM package is missing plugins directory", path="plugins")
        return

    plugin_report = validate_plugin(plugins_dir)
    for message in plugin_report.messages:
        if message.path is not None:
            message.path = f"plugins/{message.path}"
        report.add(message)


def _validate_pcm_schema(metadata: dict[str, Any], report: ValidationReport) -> bool:
    all_errors: dict[str, list[ValidationMessage]] = {}

    if "$schema" not in metadata:
        report.add_error("metadata.json must include a $schema reference")
    elif "https://go.kicad.org/pcm/schemas/v" not in metadata["$schema"]:
        report.add_error(f"unexpected schema URI: {metadata['$schema']}")

    for schema_version in _PCM_SCHEMAS:
        try:
            schema_text = (
                files("kipy.packaging.schemas")
                .joinpath(schema_version)
                .read_text(encoding="utf-8")
            )
            schema = json.loads(schema_text)
        except ValueError as ex:
            report.add_error(f"failed to read schema '{schema_version}': {ex!s}")
            continue

        validator = Draft7Validator(schema)
        errors: list[ValidationMessage] = []

        for error in sorted(
            validator.iter_errors(metadata), key=lambda current: list(current.path)
        ):
            errors.append(
                ValidationMessage(
                    message=f"schema validation error: {error.message}",
                    path=f"metadata.json{_format_schema_error_path(error.path)}",
                    level="error",
                )
            )

        # If we pass validation on any schema version we're OK
        if len(errors) == 0:
            return True

        all_errors[schema_version] = errors

    for message in all_errors[_PCM_SCHEMAS[0]]:
        report.add(message)

    return False


def _load_metadata_json(root: Path, report: ValidationReport) -> dict[str, Any] | None:
    metadata_path = root / "metadata.json"

    if not metadata_path.exists():
        report.add_error("PCM package must have a metadata.json in its root")
        return None

    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as ex:
        report.add_error(f"failed to read metadata.json: {ex!s}")
        return None
    except json.JSONDecodeError as ex:
        report.add_error(f"invalid metadata.json: {ex!s}")
        return None

    return data


def _validate_file_path(
    root: Path,
    value: str,
    report: ValidationReport,
    path: str,
    description: str,
):
    candidate = Path(value)

    if candidate.is_absolute():
        report.add_error(f"{description} path must be relative", path=path)
        return

    resolved = (root / candidate).resolve()
    if not resolved.exists():
        report.add_error(f"{candidate.name} does not exist", path=path)
        return

    if not resolved.is_file():
        report.add_error(f"{candidate.name} is not a file", path=path)
        return

    try:
        resolved.relative_to(root)
    except ValueError:
        report.add_error(f"{resolved.name} resolves outside plugin directory", path=path)


def _format_schema_error_path(path: Any) -> str:
    parts = list(path)
    output = ""

    for part in parts:
        if isinstance(part, int):
            output += f"[{part}]"
        else:
            output += f":{part}"

    return output


_REQUIREMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*")


def _parse_requirement_name(line: str) -> str | None:
    text = line.strip()

    if text == "" or text.startswith("#"):
        return None

    match = _REQUIREMENT_NAME_PATTERN.match(text)

    if match is None:
        return None

    return match.group(0)
