# OrcaSlicer MCP

An MCP server that gives Claude (or any MCP client) the full FDM pipeline:
**slice models headlessly with OrcaSlicer, manage presets, analyze G-code, and
control an Elegoo Centauri Carbon** over the local network.

Ask your assistant to *"slice this STL with my draft profile and tell me the
print time"* or *"check on the print and show me the camera"* — and it can.

## Tools

| Tool | What it does |
|---|---|
| `list_profiles` | List machine/process/filament presets (system + user), with search |
| `get_profile` | Read a preset with its full inheritance chain resolved |
| `update_profile` | Edit a user preset (system presets are read-only) |
| `gui_project_state` | Read what's open in the OrcaSlicer GUI — file + chosen presets/settings |
| `slice_model` | STL/3MF/STEP → G-code via the OrcaSlicer CLI; presets default to the open GUI project; returns time/filament/temp stats |
| `analyze_gcode` | Parse Orca G-code: time, filament, layers, and the **actual commanded temps** (M109/M190) |
| `printer_status` | Live decoded state (idle/leveling/printing/error), temps, layer progress |
| `printer_snapshot` | Webcam still — check first-layer adhesion remotely |
| `printer_attributes` | Model, firmware, mainboard id |
| `printer_files` | List files on the printer (CC2 only — CC1 firmware doesn't expose it) |
| `upload_gcode` | Upload G-code (does **not** start printing) |
| `start_print` | Start a print — checks the printer is idle first, then **verifies the job actually started** |
| `print_control` | Pause / resume / stop |

## Setup

Requirements: Python 3.10+, [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)
(tested with 2.4.1), and for printer control an Elegoo Centauri Carbon on your LAN.

```bash
git clone https://github.com/ShreddyKrueger75/orcaslicer-mcp
cd orcaslicer-mcp
uv venv --python 3.11 && uv pip install -e .
claude mcp add orcaslicer -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

Configuration is optional — sensible defaults are detected per platform:

| Env var | Default |
|---|---|
| `ORCA_SLICER_BIN` | the standard OrcaSlicer install path for your OS |
| `ORCA_SLICER_DATA` | OrcaSlicer's config dir (presets) for your OS |
| `PRINTER_HOST` | the `print_host` saved in your OrcaSlicer machine preset |
| `DEFAULT_BED_TYPE` | `Textured PEI Plate` |

## Safety model

This server can heat hardware and start multi-day prints, so the dangerous
paths are guarded:

- **Plate type is always pinned.** The Orca CLI silently defaults to
  Cool Plate (45 °C bed) — enough to detach a big part and wreck a hotend
  (ask us how we know). Every slice sets `curr_bed_type` explicitly, from the
  caller, the open GUI project, or `DEFAULT_BED_TYPE`.
- **Stats report what the machine will do**, not what the slicer intended:
  bed/nozzle temps are parsed from the `M190`/`M109` commands in the G-code.
- **`start_print` verifies.** The CC firmware silently drops start commands
  while busy; the tool refuses to fire unless the printer is idle, then polls
  until the job demonstrably starts (or reports the error code if it doesn't).
- **Bed type is whitelisted.** An unrecognized plate name would silently
  become Cool Plate; `slice_model` rejects invalid values instead.
- **Plate-clear gate.** Starting a new job while the previous one is
  COMPLETED/STOPPED (old part likely still on the plate) requires an explicit
  `plate_cleared=True` after the user confirms — otherwise the toolhead can
  crash into the finished part. Concurrent `start_print` calls are locked out.
- **`resume` only resumes paused prints** — never a stopped/errored job where
  the nozzle may be sitting in a failure.
- **Preset edits can't become code.** `update_profile` refuses the
  `post_process` key (it executes shell commands at slice time).
- **Your GUI choices win.** If a project is open in OrcaSlicer, unset slicing
  parameters come from it rather than from the model's guesses.
- `start_print`'s description instructs clients to confirm with the user —
  it heats hardware.

Scope note: this is a local, single-user tool. File-path parameters
(`model_path`, `output_dir`, `gcode_path`) operate on your filesystem with
your permissions, like any local CLI.

## How headless slicing works (the non-obvious part)

OrcaSlicer user presets are inheritance deltas (`inherits: "<parent>"`, no
`type` field) and the CLI rejects them as-is. The server resolves the full
inheritance chain into flat JSON configs, tags them with `type`, and satisfies
the CLI's compatibility check — which compares the process/filament
`compatible_printers` list against the **machine preset's `inherits` value** —
by pinning both to the machine's nearest system ancestor. Verified against the
OrcaSlicer 2.4.1 source (`src/OrcaSlicer.cpp`, ~line 2560).

## Known limits

- Centauri Carbon **CC1** firmware cannot list/delete files or report disk
  space over SDCP (CC2 can). Uploads to a full or busy printer fail with
  HTTP 500 — manage storage on the touchscreen.
- Editing profiles while the OrcaSlicer GUI is open may be overwritten when
  the GUI exits.
- Slicing timeout is 600 s per call; the CLI reports no progress.

## Credits

Printer control is [pycentauri](https://github.com/bjan/pycentauri)
(Apache-2.0). Slicing is [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)'s
own CLI. This project is MIT licensed.
