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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ValidationMessage:
    level: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"level": self.level, "message": self.message}

        if self.path is not None:
            value["path"] = self.path

        return value


@dataclass
class ValidationReport:
    root: Path
    messages: list[ValidationMessage] = field(default_factory=list)

    def add_error(self, message: str, path: str | None = None):
        self.messages.append(ValidationMessage(level="error", message=message, path=path))

    def add_warning(self, message: str, path: str | None = None):
        self.messages.append(ValidationMessage(level="warning", message=message, path=path))

    def add_info(self, message: str, path: str | None = None):
        self.messages.append(ValidationMessage(level="info", message=message, path=path))

    def add(self, message: ValidationMessage):
        self.messages.append(message)

    @property
    def errors(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.level == "error"]

    @property
    def warnings(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.level == "warning"]

    @property
    def info(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.level == "info"]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ok": self.ok,
            "errors": [m.to_dict() for m in self.errors],
            "warnings": [m.to_dict() for m in self.warnings],
            "info": [m.to_dict() for m in self.info],
        }
