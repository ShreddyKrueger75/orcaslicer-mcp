"""OrcaSlicer MCP server.

Tools: preset listing/reading/editing, headless slicing via the OrcaSlicer CLI
(with full preset-inheritance resolution), G-code analysis, reading the GUI's
open-project state, and network printer control (status, webcam snapshot,
upload, start/pause/resume/stop) for any supported printer — see backends.py.

Configuration (env vars, all optional):
  ORCA_SLICER_BIN   path to the OrcaSlicer executable
  ORCA_SLICER_DATA  path to OrcaSlicer's config dir (system/user presets)
  DEFAULT_BED_TYPE  plate used when neither the caller nor the GUI project says
  PRINTER_TYPE      moonraker | octoprint | prusalink | duet | elegoo | bambu
  PRINTER_HOST      printer IP/hostname
  PRINTER_PORT / PRINTER_API_KEY / PRINTER_USER / PRINTER_PASSWORD /
  PRINTER_SERIAL / PRINTER_ACCESS_CODE / PRINTER_SNAPSHOT_URL
                    per-protocol connection details (see printer_setup)
  ORCASLICER_MCP_CONFIG  path to the saved printer config JSON

With no printer env vars set, the server reads the printer's address and
protocol straight from your OrcaSlicer machine preset (print_host/host_type),
then falls back to its own saved config. printer_setup() reports what it found.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

import backends
from backends import NotConfigured, Target, Unsupported

_SYSTEM = platform.system()
ORCA_BIN = os.environ.get("ORCA_SLICER_BIN") or {
    "Darwin": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    "Windows": r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
}.get(_SYSTEM) or shutil.which("orca-slicer") or "orca-slicer"
ORCA_DATA = Path(os.environ.get("ORCA_SLICER_DATA") or {
    "Darwin": Path.home() / "Library/Application Support/OrcaSlicer",
    "Windows": Path(os.environ.get("APPDATA", "")) / "OrcaSlicer",
}.get(_SYSTEM, Path.home() / ".config/OrcaSlicer"))
DEFAULT_BED_TYPE = os.environ.get("DEFAULT_BED_TYPE", "Textured PEI Plate")
# Orca silently falls back to Cool Plate (45C) on an unrecognized bed type —
# that failure mode already cost a hotend, so reject bad values loudly.
# Verbatim from OrcaSlicer's BedType key map (src/libslic3r/PrintConfig.cpp).
VALID_BED_TYPES = ("Default Plate", "Cool Plate", "Textured Cool Plate",
                   "Supertack Plate", "Engineering Plate", "High Temp Plate",
                   "Textured PEI Plate")
KINDS = ("machine", "process", "filament")

mcp = FastMCP("orcaslicer")

# ---------------------------------------------------------------- profiles


def _preset_index(kind: str) -> dict[str, Path]:
    """Map preset name -> file path, user presets overriding system ones."""
    idx: dict[str, Path] = {}
    bases = [ORCA_DATA / "system"] + sorted((ORCA_DATA / "user").glob("*"))
    for base in bases:
        if not base.is_dir():
            continue
        for f in base.rglob("*.json"):
            if kind not in f.parts:
                continue
            try:
                d = json.loads(f.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if "name" in d:
                idx[d["name"]] = f
    return idx


def _resolve(name: str, kind: str, idx: dict[str, Path]) -> dict:
    """Merge a preset with its full `inherits` chain (parents first)."""
    if name not in idx:
        close = [n for n in idx if name.lower() in n.lower()][:10]
        raise ValueError(f"{kind} preset not found: {name!r}."
                         + (f" Did you mean one of {close}?" if close else ""))
    d = json.loads(idx[name].read_text())
    merged: dict = {}
    for parent in [p.strip() for p in d.get("inherits", "").split(";") if p.strip()]:
        merged.update(_resolve(parent, kind, idx))
    merged.update(d)
    return merged


def _system_ancestor(name: str, idx: dict[str, Path]) -> str:
    """Nearest preset name in the inherits chain whose file lives in system/."""
    cur = name
    while cur in idx:
        if "system" in idx[cur].parts:
            return cur
        parents = json.loads(idx[cur].read_text()).get("inherits", "")
        first = parents.split(";")[0].strip()
        if not first:
            return cur
        cur = first
    return cur


@mcp.tool()
def list_profiles(kind: str | None = None, search: str | None = None) -> dict:
    """List OrcaSlicer presets. kind: machine | process | filament | None (all).
    search: optional case-insensitive substring filter on the name.
    Returns names with their source (system or user)."""
    kinds = [kind] if kind else list(KINDS)
    out = {}
    for k in kinds:
        if k not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        items = sorted(_preset_index(k).items())
        if search:
            items = [(n, p) for n, p in items if search.lower() in n.lower()]
        out[k] = [
            {"name": n, "source": "system" if "system" in p.parts else "user"}
            for n, p in items
        ]
    return out


@mcp.tool()
def get_profile(name: str, kind: str, resolved: bool = True) -> dict:
    """Read a preset. resolved=True merges the full inheritance chain so you
    see the effective settings; resolved=False shows only the preset's own
    overrides."""
    idx = _preset_index(kind)
    if resolved:
        return _resolve(name, kind, idx)
    if name not in idx:
        raise ValueError(f"{kind} preset not found: {name!r}")
    return json.loads(idx[name].read_text())


@mcp.tool()
def update_profile(name: str, kind: str, settings: dict) -> str:
    """Set values on a USER preset (system presets are read-only — copy them in
    the OrcaSlicer GUI first). Only pass the keys you want to change.
    Note: quit the OrcaSlicer GUI first or it may overwrite the edit on exit."""
    idx = _preset_index(kind)
    if name not in idx:
        raise ValueError(f"{kind} preset not found: {name!r}")
    path = idx[name]
    if "system" in path.parts:
        raise ValueError(f"{name!r} is a system preset; edit a user copy instead.")
    if "post_process" in settings:
        # post_process runs shell commands at slice time — a preset edit must
        # never become code execution. Set it in the OrcaSlicer GUI if needed.
        raise ValueError("Refusing to set 'post_process' (executes shell "
                         "commands when slicing).")
    d = json.loads(path.read_text())
    d.update(settings)
    path.write_text(json.dumps(d, indent=4))
    return f"Updated {path} ({len(settings)} key(s))"


# ---------------------------------------------------------------- GUI state


def _gui_project() -> dict | None:
    """Settings of the project currently open in the OrcaSlicer GUI, via its
    live autosave. Returns None if no GUI project state is found."""
    conf = ORCA_DATA / "OrcaSlicer.conf"
    if not conf.is_file():
        return None
    try:
        backup = json.loads(conf.read_text()).get("app", {}).get("last_backup_path")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not backup:
        return None
    cfg = None
    for cand in ["Metadata/project_settings.config", "_temp_1.config"]:
        p = Path(backup) / cand
        if p.is_file():
            try:
                cfg = json.loads(p.read_text())
                break
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue  # GUI may be mid-write; treat as no project
    if cfg is None:
        return None
    origin = Path(backup) / "origin.txt"
    filaments = cfg.get("filament_settings_id")
    if isinstance(filaments, str):  # single-filament projects store a string
        filaments = [filaments]
    return {
        "origin_file": origin.read_text().strip() if origin.is_file() else None,
        "printer": cfg.get("printer_settings_id"),
        "process": cfg.get("print_settings_id"),
        "filaments": filaments,
        "bed_type": cfg.get("curr_bed_type"),
        "layer_height": cfg.get("layer_height"),
        "wall_loops": cfg.get("wall_loops"),
        "sparse_infill_density": cfg.get("sparse_infill_density"),
        "enable_support": cfg.get("enable_support"),
        "brim_type": cfg.get("brim_type"),
        "nozzle_temperature": cfg.get("nozzle_temperature"),
    }


@mcp.tool()
def gui_project_state() -> dict:
    """What's open in the OrcaSlicer GUI right now: source file plus the
    printer/process/filament presets and key setting overrides the user chose.
    ALWAYS check this before choosing slicing presets yourself — if the user
    has the part open, their settings win."""
    state = _gui_project()
    if state is None:
        return {"open": False,
                "note": "No live GUI project found (OrcaSlicer not running "
                        "or no project open)."}
    return {"open": True, **state}


# ---------------------------------------------------------------- slicing


def _write_cli_config(name: str, kind: str, workdir: Path,
                      machine_system_name: str | None = None) -> Path:
    """Resolve a preset to a flat JSON the Orca CLI accepts.

    The CLI's compatibility check compares the process/filament
    `compatible_printers` list against the machine preset's `inherits` value,
    so we pin both to the machine's system ancestor name.
    """
    idx = _preset_index(kind)
    d = _resolve(name, kind, idx)
    d["type"] = kind
    if kind == "machine":
        d["inherits"] = _system_ancestor(name, idx)
    else:
        d.pop("compatible_printers_condition", None)
        d.pop("compatible_prints", None)
        d.pop("compatible_prints_condition", None)
        d["compatible_printers"] = [machine_system_name]
    out = workdir / f"{kind}-{re.sub(r'[^A-Za-z0-9]+', '_', name)}.json"
    out.write_text(json.dumps(d))
    return out


def _num(v: str):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return v


def _gcode_stats(path: Path) -> dict:
    """Pull the useful metadata Orca writes into G-code. Temperatures come
    from the actual M-commands (what the machine will DO), not slicer
    comments — comments once masked a wrong-bed-temp failure."""
    text = path.read_text(errors="replace")
    stats: dict = {"file": str(path), "size_bytes": path.stat().st_size}
    numeric = {
        "filament_used_g": r"; total filament used \[g\]\s*=\s*([\d.]+)",
        "filament_used_mm": r"; filament used \[mm\]\s*=\s*([\d.]+)",
        "filament_cost": r"; total filament cost\s*=\s*([\d.]+)",
        "layer_count": r"; total layer number:\s*(\d+)",
        "max_z_height": r"; max_z_height:\s*([\d.]+)",
        "nozzle_temp": r"M109 S([\d.]+)",
        "bed_temp": r"M190 S([\d.]+)",
    }
    textual = {
        "estimated_time": r"; (?:model printing time|estimated printing time \(normal mode\))\s*[=:]\s*(.+)",
        "bed_type": r";curr_bed_type:(.+)",
        "filament_type": r"; filament_type = (.+)",
        "printer": r"; printer_settings_id = (.+)",
        "print_profile": r"; print_settings_id = (.+)",
        "filament_profile": r"; filament_settings_id = (.+)",
    }
    for key, pat in numeric.items():
        m = re.search(pat, text)
        if m:
            stats[key] = _num(m.group(1))
    for key, pat in textual.items():
        m = re.search(pat, text)
        if m:
            stats[key] = m.group(1).strip().strip('"')
    return stats


@mcp.tool()
def slice_model(
    model_path: str,
    printer: str | None = None,
    process: str | None = None,
    filament: str | None = None,
    output_dir: str | None = None,
    scale: float | None = None,
    rotate_z: float | None = None,
    arrange: bool = True,
    orient: bool = False,
    bed_type: str | None = None,
) -> dict:
    """Slice an STL/3MF/STEP file to G-code headlessly using named OrcaSlicer
    presets (see list_profiles). Any preset or bed_type left unset defaults to
    the project currently open in the OrcaSlicer GUI (see gui_project_state) —
    the user's on-screen choices win over guesses. Returns G-code path(s) plus
    numeric print time / filament estimates and the ACTUAL bed/nozzle temps
    from the G-code. orient=True lets Orca pick the orientation — leave off if
    the model is already oriented correctly."""
    model = Path(model_path).expanduser()
    if not model.is_file():
        raise ValueError(f"Model not found: {model}")

    gui = _gui_project() or {}
    printer = printer or gui.get("printer")
    process = process or gui.get("process")
    filament = filament or (gui.get("filaments") or [None])[0]
    bed_type = bed_type or gui.get("bed_type") or DEFAULT_BED_TYPE
    if bed_type not in VALID_BED_TYPES:
        raise ValueError(f"Unknown bed_type {bed_type!r} — Orca would silently "
                         f"fall back to Cool Plate (45C bed). "
                         f"Valid: {VALID_BED_TYPES}")
    missing = [n for n, v in [("printer", printer), ("process", process),
                              ("filament", filament)] if not v]
    if missing:
        raise ValueError(
            f"No {'/'.join(missing)} preset given and no open GUI project to "
            f"default from. Pass them explicitly (see list_profiles).")

    outdir = Path(output_dir).expanduser() if output_dir else model.parent / "sliced"
    outdir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="orca-mcp-"))
    try:
        machine_idx = _preset_index("machine")
        if printer not in machine_idx:
            raise ValueError(f"machine preset not found: {printer!r}. "
                             f"Known: {sorted(machine_idx)}")
        machine_sys = _system_ancestor(printer, machine_idx)
        m_json = _write_cli_config(printer, "machine", workdir)
        p_json = _write_cli_config(process, "process", workdir, machine_sys)
        f_json = _write_cli_config(filament, "filament", workdir, machine_sys)
        # The CLI silently defaults to Cool Plate (45C bed!) unless told
        # otherwise — that once cost a hotend. Bake the real plate in.
        pd = json.loads(p_json.read_text())
        pd["curr_bed_type"] = bed_type
        p_json.write_text(json.dumps(pd))

        cmd = [ORCA_BIN,
               "--load-settings", f"{m_json};{p_json}",
               "--load-filaments", str(f_json),
               "--slice", "0",
               "--arrange", "1" if arrange else "0",
               "--orient", "1" if orient else "0",
               "--ensure-on-bed",
               "--outputdir", str(outdir)]
        if scale:
            cmd += ["--scale", str(scale)]
        if rotate_z:
            cmd += ["--rotate", str(rotate_z)]
        cmd.append(str(model))

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        gcodes = sorted(outdir.glob("*.gcode"))
        if not gcodes:
            raise RuntimeError(
                f"Slicing produced no G-code (exit {r.returncode}).\n"
                f"stdout tail: {r.stdout[-2000:]}\nstderr tail: {r.stderr[-2000:]}")
        return {"presets_used": {"printer": printer, "process": process,
                                 "filament": filament, "bed_type": bed_type},
                "gcode_files": [str(g) for g in gcodes],
                "plates": [_gcode_stats(g) for g in gcodes]}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@mcp.tool()
def analyze_gcode(gcode_path: str) -> dict:
    """Parse an Orca-sliced G-code file: print time, filament use, layer count,
    the actual commanded temperatures (M109/M190), bed type, and profile names."""
    p = Path(gcode_path).expanduser()
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    return _gcode_stats(p)


# ---------------------------------------------------------------- printer


def _orca_target() -> Target | None:
    """Read the printer's address and protocol from the user's OrcaSlicer
    machine preset. Orca stores print_host/host_type when network printing is
    configured, so most users need no setup at all. The preset open in the GUI
    wins; otherwise the first user preset that has a host."""
    idx = _preset_index("machine")
    gui = _gui_project() or {}
    names = ([gui["printer"]] if gui.get("printer") in idx else []) + \
        [n for n, p in idx.items() if "system" not in p.parts]
    for name in names:
        try:
            preset = _resolve(name, "machine", idx)
        except (ValueError, json.JSONDecodeError, OSError):
            continue
        t = backends.from_orca_preset(preset, name)
        if t:
            return t
    return None


def _resolve_target(host: str | None = None) -> Target:
    """Which printer to talk to: env vars, then this server's saved config,
    then your OrcaSlicer machine preset."""
    t = backends.from_env()
    if t is None:
        cfg = backends.load_config()
        if cfg:
            t = Target(**cfg, source=f"config file ({backends.config_path()})")
    if t is None:
        t = _orca_target()
    if t is None:
        raise NotConfigured(
            "No printer configured. Call printer_setup() — it reports what it "
            "can find on the network and in your OrcaSlicer presets — then ASK "
            "THE USER which printer they have (and its IP if it wasn't found) "
            "and call configure_printer().")
    if host:
        t.host = host
    return t


async def _backend(host: str | None = None) -> backends.Backend:
    return backends.make(_resolve_target(host))


@mcp.tool()
async def printer_setup(host: str | None = None) -> dict:
    """Find out which printer we can talk to, and what's still needed.

    Call this first when printer tools report no configuration, or when the
    user changes hardware. It reports the current config, any printer found in
    your OrcaSlicer presets, and anything answering on the network (Elegoo
    printers self-announce; pass host= to probe a specific IP).

    If nothing usable is found, ASK THE USER which printer they own and its IP
    address, then call configure_printer(). Never guess the printer type — the
    wrong protocol can mean wrong temperatures on real hardware."""
    out: dict = {"supported_types": {
        "moonraker": "Klipper machines (Voron, RatRig, Sovol, Creality K1, "
                     "Elegoo Neptune 4...). Needs: host, optional api_key.",
        "octoprint": "Any printer behind OctoPrint. Needs: host, api_key "
                     "(OctoPrint > Settings > API).",
        "prusalink": "Prusa MK4/MK3.9/XL/MINI/CORE One. Needs: host, password "
                     "(printer screen: Settings > Network > PrusaLink), "
                     "user defaults to 'maker'.",
        "duet": "Duet 2/3 (RepRapFirmware). Needs: host, password if set.",
        "elegoo": "Elegoo Centauri Carbon. Needs: host only.",
        "bambu": "Bambu Lab P1/X1/A1/H2 in LAN mode (experimental). Needs: "
                 "host, serial, access_code (printer: Settings > Network > "
                 "LAN Only Mode) and the [bambu] extra installed.",
    }}
    try:
        t = _resolve_target(host)
        out["configured"] = t.redacted()
        out["configured_from"] = t.source
    except NotConfigured as e:
        out["configured"] = None
        out["note"] = str(e)
    except Unsupported as e:
        out["configured"] = None
        out["note"] = str(e)

    orca = []
    idx = _preset_index("machine")
    for name, p in idx.items():
        if "system" in p.parts:
            continue
        try:
            preset = _resolve(name, "machine", idx)
        except Exception:
            continue
        if not preset.get("print_host"):
            continue
        entry = {"preset": name, "print_host": preset["print_host"],
                 "host_type": preset.get("host_type")}
        raw = (preset.get("host_type") or "").lower()
        if raw in backends.ORCA_HOST_TYPE:
            entry["type"] = backends.ORCA_HOST_TYPE[raw]
        elif raw in backends.ORCA_HOST_TYPE_UNSUPPORTED:
            entry["unsupported"] = backends.ORCA_HOST_TYPE_UNSUPPORTED[raw]
        orca.append(entry)
    out["orcaslicer_presets"] = orca

    found = await backends.elegoo_broadcast()
    seen = {f["host"] for f in found}
    candidates = ([host] if host else []) + [e["print_host"] for e in orca]
    for h in candidates:
        if h in seen:
            continue
        hit = await backends.probe_host(h)
        if hit:
            found.append(hit)
            seen.add(h)
    out["found_on_network"] = found
    out["next_step"] = (
        "Ready — printer tools will work." if out.get("configured") else
        "Ask the user which printer they have (and its IP if not listed in "
        "found_on_network), then call configure_printer(). If found_on_network "
        "lists a type, confirm it with the user rather than assuming.")
    return out


@mcp.tool()
async def configure_printer(printer_type: str, host: str,
                            api_key: str | None = None,
                            user: str | None = None,
                            password: str | None = None,
                            serial: str | None = None,
                            access_code: str | None = None,
                            port: int | None = None,
                            save: bool = True) -> dict:
    """Point the server at a printer, verify it answers, and save it.

    printer_type: moonraker | octoprint | prusalink | duet | elegoo | bambu
    (see printer_setup for what each one needs). Verifies by reading live
    status before saving — a config that can't connect is never written.
    The file is written user-only (0600) since it holds keys/access codes."""
    if printer_type not in backends.TYPES:
        raise ValueError(f"printer_type must be one of {backends.TYPES}")
    t = Target(type=printer_type, host=host, port=port, api_key=api_key,
               user=user, password=password, serial=serial,
               access_code=access_code, source="configure_printer")
    b = backends.make(t)
    try:
        status = await b.status()
        try:
            attrs = await b.attributes()
        except Exception:
            attrs = {}
    except Exception as e:
        raise RuntimeError(
            f"Could not talk to a {printer_type} printer at {host}: {e}. "
            "Nothing was saved. Check the address, the credentials, and that "
            "printer_setup's found_on_network agrees with this type.") from e
    finally:
        await b.close()
    out = {"ok": True, "printer": t.redacted(), "attributes": attrs,
           "status": status}
    if save:
        out["saved_to"] = str(backends.save_config(t))
    return out


@mcp.tool()
async def printer_status(host: str | None = None) -> dict:
    """Live printer status: normalized state (idle/heating/printing/paused/
    complete/stopped/error/busy), temps, layer progress. Works with any
    supported printer; `native_state` keeps the firmware's own wording."""
    b = await _backend(host)
    try:
        s = await b.status()
        return {"backend": b.name, **s}
    finally:
        await b.close()


@mcp.tool()
async def printer_snapshot(host: str | None = None) -> Image:
    """Grab a still from the printer's camera — use it to check first-layer
    adhesion and mid-print health remotely. Not every printer has one."""
    b = await _backend(host)
    try:
        img = await b.snapshot()
    finally:
        await b.close()
    fmt = "png" if img[:4] == b"\x89PNG" else "jpeg"
    return Image(data=img, format=fmt)


@mcp.tool()
async def printer_attributes(host: str | None = None) -> dict:
    """Printer identity and firmware info — useful when debugging protocol
    quirks or confirming the server is talking to the right machine."""
    b = await _backend(host)
    try:
        return await b.attributes()
    finally:
        await b.close()


@mcp.tool()
async def printer_files(host: str | None = None) -> dict:
    """List G-code files stored on the printer. Some firmwares don't allow it
    (notably the Elegoo Centauri Carbon CC1)."""
    b = await _backend(host)
    try:
        return {"backend": b.name, "files": await b.files()}
    except Unsupported as e:
        return {"backend": b.name, "supported": False, "reason": str(e)}
    finally:
        await b.close()


@mcp.tool()
async def upload_gcode(gcode_path: str, host: str | None = None,
                       remote_name: str | None = None) -> str:
    """Upload a G-code file to the printer. Does NOT start printing —
    use start_print for that."""
    src = Path(gcode_path).expanduser()
    if not src.is_file():
        raise ValueError(f"File not found: {gcode_path}")
    b = await _backend(host)
    try:
        name = await b.upload(src, remote_name or src.name)
        return f"Uploaded to the {b.name} printer as {name} " \
               f"({src.stat().st_size} bytes). Not printing yet."
    finally:
        await b.close()


_start_lock = None  # created lazily; module import happens outside a loop


@mcp.tool()
async def start_print(filename: str, host: str | None = None,
                      plate_cleared: bool = False) -> dict:
    """Start printing a file already on the printer (see upload_gcode).
    Physical action — confirm with the user before calling. Refuses unless the
    printer is idle, then polls until it demonstrably starts, because some
    firmwares silently drop start commands sent while busy.

    If the previous job finished or was stopped, the old part may still be on
    the plate and the toolhead would crash into it: ask the user to confirm the
    plate is empty, then pass plate_cleared=True."""
    import asyncio
    global _start_lock
    if _start_lock is None:
        _start_lock = asyncio.Lock()
    if _start_lock.locked():
        raise RuntimeError("Another start_print is already in progress.")
    async with _start_lock:
        b = await _backend(host)
        try:
            before = await b.status()
            state = before["state"]
            if state in backends.PLATE_DIRTY_STATES and not plate_cleared:
                raise RuntimeError(
                    f"The last job is {state} ({before['native_state']}) — the "
                    "old part may still be on the plate and the toolhead would "
                    "crash into it. Confirm with the user that the plate is "
                    "empty, then call again with plate_cleared=True.")
            if state not in backends.STARTABLE_STATES:
                raise RuntimeError(
                    f"Printer is not idle (state: {state} / "
                    f"{before['native_state']}). Wait for it to finish.")
            await b.start(filename)
            deadline = time.monotonic() + 30
            last = state
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                now = await b.status()
                last = now["state"]
                if last == backends.ERROR:
                    raise RuntimeError(
                        f"Printer entered ERROR after start "
                        f"(error={now['print']['error_code']}).")
                if last in backends.ACTIVE_STATES:
                    return {"started": True, "filename": filename,
                            "backend": b.name, "state": last,
                            "native_state": now["native_state"],
                            "note": "Some printers run a calibration routine "
                                    "for several minutes before extruding."}
            raise RuntimeError(
                f"start_print was issued but the printer never left {last} "
                "within 30s — the command was likely dropped. Check the "
                "printer and retry.")
        finally:
            await b.close()


@mcp.tool()
async def print_control(action: str, host: str | None = None) -> dict:
    """Pause, resume, or stop the current print. action: pause|resume|stop.
    resume only acts on a paused print — never on a stopped or errored job,
    where the nozzle may be sitting in a failure."""
    if action not in ("pause", "resume", "stop"):
        raise ValueError("action must be pause, resume, or stop")
    b = await _backend(host)
    try:
        state = (await b.status())["state"]
        if action == "resume" and state != backends.PAUSED:
            raise RuntimeError(
                f"Printer is {state}, not paused — refusing to resume. Resuming "
                "a stopped or failed job can drive the nozzle into a blob.")
        if action == "pause" and state not in (backends.PRINTING,
                                               backends.HEATING,
                                               backends.BUSY):
            raise RuntimeError(f"Printer is {state} — nothing to pause.")
        await getattr(b, action)()
        return {"ok": True, "action": action, "backend": b.name}
    finally:
        await b.close()


if __name__ == "__main__":
    mcp.run()
