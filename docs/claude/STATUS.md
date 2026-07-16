# STATUS — OrcaSlicer MCP

## Current state (2026-07-15)
- v0.1.0 built and self-tested. Single-file server (`server.py`), Python 3.11 venv via uv.
- Slicing verified end-to-end: cube.stl → plate_1.gcode with Centauri Carbon presets.
- Printer status verified live against the real CC1 at 192.168.4.34.
- Not yet exercised on hardware: upload_gcode / start_print (needs John present at the printer).

## Project canonical facts
- Stack: Python 3.11, FastMCP (`mcp` package), pycentauri (Apache-2.0) for SDCP printer control.
- OrcaSlicer 2.4.1 at /Applications/OrcaSlicer.app; config at ~/Library/Application Support/OrcaSlicer.
- Printer: Elegoo Centauri Carbon (CC1) at 192.168.4.34, SDCP over WebSocket :3030.
- Key gotcha: CLI compatibility check = process `compatible_printers` vs machine `inherits` (see README).

## Next
- Live test upload_gcode + start_print with John at the printer.
- Register the server: `claude mcp add orcaslicer -- <abs>/.venv/bin/python <abs>/server.py`
