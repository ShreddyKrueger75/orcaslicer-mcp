# Changelog

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
