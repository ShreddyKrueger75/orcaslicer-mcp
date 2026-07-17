# STATUS — OrcaSlicer MCP

## Current state (2026-07-16)
- v0.4.0: watch_print monitoring, multi-material/plate/skip slicing, end-to-end fixture tests (real socket: digest auth, Duet sessions), GitHub Actions CI + CONTRIBUTING. 40 self-tests green.
- v0.3.1: device-agnostic printer control, hardened by an adversarial pass (gcode injection, wrong-printer selection, plate gate on Duet/OctoPrint, Duet sessions) (moonraker/octoprint/prusalink/duet/elegoo/bambu) via `backends.py`; zero-config from OrcaSlicer machine presets; `printer_setup`/`configure_printer` flow. 28 self-tests green (`python test_server.py`).
- Public: https://github.com/ShreddyKrueger75/orcaslicer-mcp
- Live-verified on the CC1 through the new backend layer (status/discovery mid-print).

## Project canonical facts
- Stack: Python 3.11, FastMCP (`mcp`), httpx for REST protocols, pycentauri (Apache-2.0) for Elegoo SDCP, optional bambulabs-api (MIT). MIT licensed.
- OrcaSlicer 2.4.1; paths via ORCA_SLICER_BIN / ORCA_SLICER_DATA; printer via OrcaSlicer preset (print_host/host_type) → saved config → PRINTER_* env.
- Printer: Elegoo Centauri Carbon (CC1) firmware V1.4.46, SDCP over WebSocket :3030. CC1 firmware: no file list/delete/disk info.
- Key gotchas: CLI compat check = process `compatible_printers` vs machine `inherits`; CLI silently defaults curr_bed_type to Cool Plate (45C) — always pinned; bed-type list must match Orca's BedType enum (7 plates).

## Next
- Only the Elegoo backend is hardware-verified; the REST backends are docs+mock-tested. Real-hardware reports welcome.
- Hardware-test start_print verification on a fresh job (unit-tested; live start not exercised since the fix).
