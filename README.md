# OrcaSlicer MCP

An MCP server that gives Claude (or any MCP client) the full FDM pipeline:
**slice models headlessly with OrcaSlicer, manage presets, analyze G-code, and
control your printer** over the local network — Klipper, OctoPrint, Prusa,
Duet, Elegoo, or Bambu.

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
| `printer_setup` | What printer we can talk to and what's still needed — run this first |
| `configure_printer` | Point the server at a printer, verify it answers, save it |
| `printer_status` | Live normalized state (idle/heating/printing/paused/error), temps, layer progress |
| `printer_snapshot` | Webcam still — check first-layer adhesion remotely |
| `printer_attributes` | Model, firmware, mainboard id |
| `printer_files` | List files on the printer (where the firmware allows it) |
| `upload_gcode` | Upload G-code (does **not** start printing) |
| `start_print` | Start a print — checks the printer is idle first, then **verifies the job actually started** |
| `print_control` | Pause / resume / stop |

## Printer support

Slicing works for every printer OrcaSlicer supports. Printer *control* speaks
these protocols:

| Type | Printers | Needs | Verified |
|---|---|---|---|
| `moonraker` | Klipper — Voron, RatRig, Sovol, Creality K1, Neptune 4… | host, api_key (only if Moonraker requires one) | docs + mock tests |
| `octoprint` | Anything behind OctoPrint (most Marlin printers) | host, api_key (Settings → API) | docs + mock tests |
| `prusalink` | Prusa MK4 / MK3.9 / XL / MINI / CORE One | host, password (Settings → Network), user `maker` | docs + mock tests |
| `duet` | Duet 2/3 (RepRapFirmware) | host, password if set | docs + mock tests |
| `elegoo` | Elegoo Centauri Carbon | host | **live hardware** |
| `bambu` | Bambu Lab P1/X1/A1/H2, LAN mode — experimental | host, serial, access_code, `[bambu]` extra | docs only |

**You probably don't need to configure anything.** If you already set up
network printing in OrcaSlicer, the server reads the printer's address and
protocol from your machine preset (`print_host` / `host_type`). Otherwise ask
your assistant to run `printer_setup` — it scans, reports what it finds, and
tells the assistant to ask you which printer you own rather than guessing.

Honest scope: only the Elegoo path has been exercised on real hardware by this
project. The rest were built against each protocol's official docs and source
(and fact-checked against them), with mock-transport tests asserting the parts
that can bite — including that **upload never starts a print** (asserted for
Moonraker, OctoPrint and PrusaLink, whose APIs have an auto-start flag to
suppress; Duet, Elegoo and Bambu have no such flag — starting is a separate
call by construction). Reports from
real machines are welcome.

Adding a protocol is one small class in `backends.py`.

## Setup

Requirements: Python 3.10+, [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)
(tested with 2.4.1), and — for printer control — a supported printer on your LAN.

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
| `PRINTER_TYPE` | read from your OrcaSlicer machine preset's `host_type` |
| `PRINTER_HOST` | the `print_host` saved in your OrcaSlicer machine preset |
| `PRINTER_PORT`, `PRINTER_API_KEY`, `PRINTER_USER`, `PRINTER_PASSWORD`, `PRINTER_SERIAL`, `PRINTER_ACCESS_CODE` | per-protocol; also readable from the preset or `configure_printer` |
| `PRINTER_SNAPSHOT_URL` | OctoPrint webcam URL (default `http://<host>:8080/?action=snapshot`) |
| `ORCASLICER_MCP_CONFIG` | `~/.config/orcaslicer-mcp/printer.json` (written 0600) |
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
- **`start_print` verifies.** Some firmwares silently drop start commands sent
  while busy; the tool refuses to fire unless the printer is idle, then polls
  until the job demonstrably starts (or reports the error code if it doesn't).
- **Upload never prints.** Every backend suppresses its protocol's
  start-on-upload flag, with tests per protocol that has one.
- **Filenames are data, never syntax.** A name reaches firmware inside a G-code
  argument (Duet: `M32 "name"`) and inside URLs; `;` or `"` would let a crafted
  name append arbitrary G-code (`M109 S300`) or escape the upload directory.
  Names are validated before they leave the server.
- **No guessing which printer.** If several printers are configured and nothing
  says which you mean, the server asks instead of picking; an unknown `host=`
  is refused rather than driven with another printer's protocol and credentials.
- **Bed type is whitelisted** against OrcaSlicer's own `BedType` enum (all 7
  plates, Supertack included). An unrecognized name would silently become Cool
  Plate; `slice_model` rejects invalid values instead.
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
- OctoPrint and PrusaLink don't report layer counts over their APIs; Duet has
  no standard camera endpoint. Tools say so instead of failing obscurely.
- The plate-clear gate relies on the printer reporting a finished job. Duet
  (`job.lastFileName`) and OctoPrint (100% progress) are inferred; a *cancelled*
  OctoPrint job is indistinguishable from idle over its API, so that one case
  won't trip the gate.
- On Windows the saved config can't be locked to your user with `chmod`; it
  inherits the folder's ACL. Prefer `PRINTER_*` env vars if that matters.
- Cloud host types (PrusaConnect, CrealityPrint, Obico, SimplyPrint,
  3DPrinterOS) aren't supported — point the server at the printer's own IP.
- Editing profiles while the OrcaSlicer GUI is open may be overwritten when
  the GUI exits.
- Slicing timeout is 600 s per call; the CLI reports no progress.

## Credits

Elegoo control is [pycentauri](https://github.com/bjan/pycentauri) (Apache-2.0);
optional Bambu control is [bambulabs-api](https://github.com/mchrisgm/bambulabs_api)
(MIT); other protocols are spoken directly over HTTP with
[httpx](https://github.com/encode/httpx) (BSD-3-Clause). Slicing is
[OrcaSlicer](https://github.com/SoftFever/OrcaSlicer)'s own CLI. This project is
MIT licensed.
