# kicad-jlcpcb-tools project instructions

## Code quality

All commits must be ruff-clean. Run `ruff check` and `ruff format --check` before committing.
When making changes to a file, only reformat lines you are intentionally changing — avoid running
`ruff format` across an entire file unless that is the explicit goal, as it creates unrelated diff noise.

## Python version compatibility

KiCad's internal Python interpreter is **Python 3.9**. All code must be 3.9-compatible:

- Use `Optional[X]` instead of `X | None`
- Use `List[X]`, `Dict[K, V]`, `Tuple[X, ...]` from `typing` instead of built-in generics (`list[X]`, `dict[K, V]`, `tuple[X, ...]`)
- No `match`/`case` statements
- No use of `typing.Self`, `typing.TypeAlias`, `typing.ParamSpec`, or other 3.10+ additions
