# Changelog

## 0.3.0 — 2026-07-16
- **Device-agnostic printer control.** Printer tools now work with Klipper/Moonraker, OctoPrint, PrusaLink (Prusa MK4/XL/MINI/CORE One), Duet/RepRapFirmware, Elegoo Centauri Carbon, and — experimentally, via the `[bambu]` extra — Bambu Lab in LAN mode. Each protocol is a small adapter in the new `backends.py`.
- **Zero-config for OrcaSlicer users**: the printer's address and protocol are read from your machine preset (`print_host` / `host_type`). Falls back to a saved config (`~/.config/orcaslicer-mcp/printer.json`, written 0600) and `PRINTER_*` env vars.
- New `printer_setup` (find the printer, report what's missing, tell the assistant to *ask* which printer you own rather than guess) and `configure_printer` (verifies a live connection before saving).
- `printer_status` returns one normalized shape for every printer — shared state vocabulary (idle/heating/printing/paused/complete/stopped/error/busy) plus the firmware's own `native_state`.
- Safety generalized across protocols: idle check + start verification, plate-clear gate, resume-only-when-paused, and **upload never starts a print** (each backend suppresses its protocol's start-on-upload flag; asserted by tests).
- Fix: the bed-type whitelist was missing real plates ("Supertack Plate", "Textured Cool Plate", "Default Plate"), which would have blocked Bambu A1/H2 users. Now taken verbatim from OrcaSlicer's `BedType` enum.
- Tests: 14 self-checks including mock-transport tests per protocol; live-verified on the Centauri Carbon.

## 0.2.0 — 2026-07-16
- Security/safety hardening from adversarial review: bed_type whitelist (invalid names would silently become Cool Plate), plate-clear confirmation gate + mutex on `start_print`, `resume` restricted to paused prints, `post_process` key rejected in `update_profile` (slice-time shell execution), snapshot `save_path` removed (arbitrary-file-write), corrupt-JSON tolerance in GUI state reads, graceful `printer_files` on CC1.
- `slice_model` presets/bed type now default to the project open in the OrcaSlicer GUI (`gui_project_state` tool added) — on-screen choices win.
- `start_print` refuses to fire unless the printer is idle and verifies the job actually started (the firmware silently drops commands while busy).
- `printer_status` returns structured, typed JSON with decoded state names (idle/auto_leveling/printing/error...) instead of stringified objects.
- `analyze_gcode`/slice stats: numeric fields are numbers; temperatures parse from the actual `M190`/`M109` commands.
- New tools: `printer_snapshot` (webcam image), `printer_attributes`, `list_profiles(search=...)`.
- Upload failures explain the CC1 busy/storage-full ambiguity.
- Portable config via `ORCA_SLICER_BIN` / `ORCA_SLICER_DATA` / `PRINTER_HOST` / `DEFAULT_BED_TYPE`; Windows/Linux path defaults.
- MIT license; public release.

## 0.1.0 — 2026-07-15
- Initial: headless slicing with preset-inheritance resolution, profile management, G-code analysis, Centauri Carbon control via pycentauri.
- Fix: pin `curr_bed_type` when slicing (CLI's silent Cool Plate default caused a 45C bed on a textured-PEI print — adhesion failure).
