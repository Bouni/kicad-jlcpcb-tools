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

import argparse
import sys

from kipy.packaging.validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kicad-python-packager",
        description="Utilities for packaging KiCad Python plugins.",
    )
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validates plugins and PCM packages",
    )
    validate_parser.add_argument(
        "path",
        help="Path to plugin directory, PCM package directory, or PCM .zip archive",
    )

    args = parser.parse_args(argv)

    if args.command != "validate":
        parser.print_help(sys.stderr)
        return 2

    report = validate(args.path)

    print(f"Validating path: {report.root!s}")

    for message in report.messages:
        output = f"[{message.level}] {message.message}"
        if message.path is not None:
            output = f"{output} ({message.path})"
        print(output)

    if report.ok:
        print("Validation passed.")
    else:
        print(f"Validation failed with {len(report.errors)} error(s).")

    return 0 if report.ok else 2
