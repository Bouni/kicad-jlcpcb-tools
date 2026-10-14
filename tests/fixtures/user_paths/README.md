# Schematic fixtures for the user-path tests

`tests/test_kicad_user_paths_native.py` exports a board's assignments into
these schematics. Each folder holds a minimal project named `user_paths` for
one KiCad major version: a `.kicad_pro` and a `.kicad_sch` with two resistors
on the root sheet. R1 already carries an `LCSC` field and R2 has none. The test
copies the folder for the running KiCad, so each lane writes into its own file
format.

## How they were made

- `kicad7`, `kicad8` and `kicad9` were written by hand in the layout of that
  release's schematic writer: `eeschema/sch_plugins/kicad/sch_sexpr_plugin.cpp`
  at KiCad 7.0.11, and `eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr.cpp` at
  8.0.9 and 9.0.9. The file version stamp, property layout and indentation
  follow the writer, and the placed symbols have the same shape as those in
  the schematics KiCad ships at each tag.
- Their library symbol, `UserPaths:R`, is cached as each release writes it:
  run through that release's `kicad-cli sym upgrade --force`, the symbol comes
  back unchanged.
- `kicad10` is KiCad 10.0.7's own output: `kicad-cli sch upgrade --force` of
  the `kicad9` schematic. KiCad 10 writes the Datasheet and the pin names as
  empty where KiCad 9 wrote `~`. Its project lists the root sheet under
  `top_level_sheets`, as KiCad 10 projects do, so the test links each
  footprint through the root sheet as KiCad 10's Update PCB does.
- Each release's `kicad-cli sch export netlist` reads its own folder's
  schematic, and the test checks that before it exports.
- The project files hold only the metadata the test needs.

## UUIDs

The UUIDs are fixed and are the same in all four folders. The test links the
board's footprints to the symbols through them, so change them only together
with `ROOT_SHEET` and `SYMBOLS` in the test.
