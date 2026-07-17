# Changelog

## 0.4.0 — 2026-07-16
- **Print monitoring:** new `watch_print` tool — a bounded poll (≤300s) that returns on a state change, a target state, or an error, with the progress moved during the wait. Follow a multi-day job without hand-writing status loops. Backend-agnostic; no backend changes.
- **Slicing depth:** `slice_model` gains `filaments` (multi-material list), `filament_ids` (object→slot map, "1,2,1"), `plate` (slice one plate), and `skip_objects` ("3,5"). The singular `filament` parameter still works, and `presets_used` keeps a `filament` key for single-material prints alongside the new `filaments` list. `filament_ids` is bounds-checked against the number of filaments. Reuses the existing preset-inheritance resolver per filament. AMS slot mapping deferred.
- **Backends proven end-to-end:** new `test_fixtures.py` runs a stdlib HTTP server on an ephemeral socket so the real httpx client is exercised against it — proving PrusaLink Digest auth, the Duet session/disconnect lifecycle, and real multipart uploads (which `MockTransport` can't reach). Upload-never-prints is now asserted over a real socket for every REST protocol.
- **Polish:** GitHub Actions CI (Python 3.11/3.12) running the suite on every push/PR, a tests badge, `CONTRIBUTING.md` (how to add a backend + the safety rules), issue templates, and an honest example session in the README.
- Tests: 28 → 40.

## 0.3.1 — 2026-07-16
Adversarial review of the v0.3.0 backends (24 findings, all fixed):
- **Security fix (Duet):** a filename was interpolated straight into `M32 "…"`, so a crafted name (`a.gcode"; M109 S300 ; "`) could append arbitrary G-code — e.g. drive the nozzle to 300 °C. Filenames are now validated everywhere they reach firmware or a URL path (also closes a PrusaLink upload path-traversal).
- **Wrong-printer safety:** with two printers set up in OrcaSlicer and no project open, the server picked one by filesystem order. It now refuses and asks. An explicit `host=` that matches no known printer is refused instead of being driven with another printer's protocol/credentials.
- **Plate-clear gate now fires on Duet and OctoPrint:** both report a finished job as plain "idle", so the gate never triggered and the toolhead could crash into the previous part. Detected via `job.lastFileName` (Duet) and 100% progress (OctoPrint).
- **Duet session exhaustion:** every call opened a new RRF session and never released it, wedging the printer's small session pool after ~8 calls. One session per connection now, released on close.
- Robustness: Moonraker with Klippy disconnected (null result) or null temps, OctoPrint 409 when disconnected, PrusaLink null file list, Duet reporting the hotend as the bed when no bed exists, Bambu enum stringification across library versions, corrupt/hand-edited config files.
- Packaging: declare `py-modules` so an editable install exposes the modules.
- Docstrings: `plate_cleared` must come from the user, never inferred; `configure_printer` marked high-trust; precise refusal conditions.
- Tests: 14 → 28, one per finding.

## 0.3.0 — 2026-07-16
- **Device-agnostic printer control.** Printer tools now work with Klipper/Moonraker, OctoPrint, PrusaLink (Prusa MK4/XL/MINI/CORE One), Duet/RepRapFirmware, Elegoo Centauri Carbon, and — experimentally, via the `[bambu]` extra — Bambu Lab in LAN mode. Each protocol is a small adapter in the new `backends.py`.
- **Zero-config for OrcaSlicer users**: the printer's address and protocol are read from your machine preset (`print_host` / `host_type`). Falls back to a saved config (`~/.config/orcaslicer-mcp/printer.json`, written 0600) and `PRINTER_*` env vars.
- New `printer_setup` (find the printer, report what's missing, tell the assistant to *ask* which printer you own rather than guess) and `configure_printer` (verifies a live connection before saving).
- `printer_status` returns one normalized shape for every printer — shared state vocabulary (idle/heating/printing/paused/complete/stopped/error/busy) plus the firmware's own `native_state`. **Breaking:** replaces v0.2.0's Elegoo-shaped keys — `state_code`, `speed_pct`, `task_id`, `z_offset` are gone; `elapsed_s`/`total_s` became `time.elapsed_s`/`time.remaining_s`; `fans_pct`/`position` remain but only on printers that report them.
- **Breaking:** `start_print`'s Elegoo-only `auto_leveling` parameter is gone; auto-leveling stays on (the firmware default) and the tool now works the same on every backend.
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
