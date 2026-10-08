# Vendored kicad-python (kipy)

KiCad 10.99 removed the SWIG `pcbnew` module, so the plugin talks to KiCad
through the IPC API when it runs as an IPC plugin (see `plugin.json`). The
released `kicad-python` 0.8.0 cannot decode the 10.99 protocol (for example
footprint `ReferencePoint` items) and has no design-variant API, so this
directory holds a build of the development branch instead.

- kicad-python: `87b945a0d56b26a78bf547db9771ff9bf3a057ea` (0.9.0.dev0, MIT, see `LICENSE`)
- protobuf sources: KiCad `api/proto` at `80ef6f30834e00bdbe57f166489bf47a45a76713`
  (10.99.0-5061-g80ef6f3083), generated with protoc 29.0 (`grpcio-tools==1.70.0`)
  so the modules run on protobuf 5.29 as kipy requires
- local change: `board_types.py` no longer imports `DrillChartAlign`, which that
  KiCad revision replaced with `HorizontalAlignment`

Runtime dependencies are listed in the plugin's `requirements.txt`; KiCad installs
them into the plugin's virtual environment.

## Regenerating

```sh
git clone https://gitlab.com/kicad/code/kicad-python.git && cd kicad-python
git submodule update --init --depth 1 kicad
git -C kicad fetch --depth 1 origin <kicad-commit> && git -C kicad checkout <kicad-commit> -- api/proto
python -m venv tools && tools/bin/pip install grpcio-tools==1.70.0 "protobuf>=5.29,<6" protoletariat
# tools/generate_protos.py expects `protoc` and `protol` on PATH; wrap
# `tools/bin/python -m grpc_tools.protoc` as `protoc` and drop --mypy_out.
python tools/generate_protos.py
printf 'KICAD_API_VERSION = "<git describe of the KiCad build>"\n' > kipy/kicad_api_version.py
```

Then replace this directory with `kipy/`, keeping this file and `LICENSE`.
