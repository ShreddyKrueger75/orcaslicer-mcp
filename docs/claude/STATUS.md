# STATUS — OrcaSlicer MCP

## Current state (2026-07-16)
- v0.2.0: GUI-project-aware slicing, verified start_print, structured printer status, webcam snapshot, portable env config. Self-tests green (`python test_server.py`).
- Published publicly: https://github.com/ShreddyKrueger75/orcaslicer-mcp
- Live-verified read-only against the CC1 mid-print (layer progress, temps, decoded states).

## Project canonical facts
- Stack: Python 3.11, FastMCP (`mcp` package), pycentauri (Apache-2.0) for SDCP printer control. MIT licensed.
- OrcaSlicer 2.4.1; paths configurable via ORCA_SLICER_BIN / ORCA_SLICER_DATA; printer via PRINTER_HOST (defaults to machine preset print_host).
- Printer: Elegoo Centauri Carbon (CC1), SDCP over WebSocket :3030. CC1 firmware: no file list/delete/disk info over SDCP.
- Key gotchas: CLI compat check = process `compatible_printers` vs machine `inherits`; CLI silently defaults curr_bed_type to Cool Plate (45C) — always pinned now.

## Next
- Hardware-test start_print verification path on a fresh job (implemented, unit-tested; live start not exercised since the fix — printer was mid-print).
