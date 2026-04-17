# kicad-jlcpcb-tools project instructions

## Code quality

All commits must be ruff-clean. Run `ruff check` and `ruff format --check` before committing.
When making changes to a file, only reformat lines you are intentionally changing.

## Python version compatibility

KiCad's internal Python interpreter is **Python 3.9**. All code that is executed in the
plugin must be 3.9-compatible.

- Use `Optional[X]` instead of `X | None`
- No `match`/`case` statements
- No use of `typing.Self`, `typing.TypeAlias`, `typing.ParamSpec`, or other 3.10+ additions

Note that files in the db_build directory use python >= 3.10, that is executed as a
github action and is not subject to the Python 3.9 guidance.
