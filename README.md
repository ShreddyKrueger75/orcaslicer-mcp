# OrcaSlicer MCP

MCP server that gives Claude full control of the 3D-printing pipeline on this Mac:
slice models headlessly with OrcaSlicer, manage presets, analyze G-code, and drive
the Elegoo Centauri Carbon over the local network.

## Tools

| Tool | What it does |
|---|---|
| `list_profiles` | List machine/process/filament presets (system + user) |
| `get_profile` | Read a preset, with full inheritance resolved |
| `update_profile` | Edit a user preset (system presets are read-only) |
| `slice_model` | STL/3MF → G-code via the OrcaSlicer CLI using named presets; returns time/filament estimates |
| `analyze_gcode` | Parse an Orca G-code file for time, filament, layers, temps |
| `printer_status` | Live temps, progress, current job (SDCP via pycentauri) |
| `printer_files` | List files on printer (CC2 only — CC1 firmware doesn't support it) |
| `upload_gcode` | Upload G-code to the printer (does not start printing) |
| `start_print` | Start a print — physical action, confirm with the user first |
| `print_control` | Pause / resume / stop the current print |

Printer host defaults to the `print_host` in the user machine preset
(currently `192.168.4.34`); every printer tool takes an optional `host` override.

## Setup

```bash
uv venv --python 3.11 && uv pip install -e .
claude mcp add orcaslicer -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

## How slicing works (the non-obvious part)

OrcaSlicer user presets are deltas (`inherits: "<system preset>"`, no `type` field)
and the CLI rejects them as-is. The server resolves the full inheritance chain into
flat JSON configs, tags them with `type`, and pins compatibility: the CLI compares
the process/filament `compatible_printers` list against the **machine preset's
`inherits` value**, so both are set to the machine's nearest system ancestor name.
Verified against OrcaSlicer 2.4.1 source (`src/OrcaSlicer.cpp` ~line 2560).

## Known limits

- `printer_files` fails on the CC1 (SDCP Cmd 258 disabled in firmware).
- Editing profiles while the OrcaSlicer GUI is open may be overwritten on GUI exit.
- Slicing timeout is 600 s per call.
