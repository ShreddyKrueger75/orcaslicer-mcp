"""OrcaSlicer MCP server.

Tools: preset listing/reading/editing, headless slicing via the OrcaSlicer CLI
(with full preset-inheritance resolution), G-code analysis, reading the GUI's
open-project state, and Elegoo Centauri Carbon printer control via pycentauri
(status, webcam snapshot, upload, start/pause/resume/stop).

Configuration (env vars, all optional):
  ORCA_SLICER_BIN   path to the OrcaSlicer executable
  ORCA_SLICER_DATA  path to OrcaSlicer's config dir (system/user presets)
  PRINTER_HOST      printer IP; default: print_host from your machine preset
  DEFAULT_BED_TYPE  plate used when neither caller nor GUI project says:
                    "Cool Plate" | "Engineering Plate" | "High Temp Plate" |
                    "Textured PEI Plate"
"""

import base64
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
VALID_BED_TYPES = ("Cool Plate", "Engineering Plate", "High Temp Plate",
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


def _default_host() -> str | None:
    """PRINTER_HOST env var, else first print_host in user machine presets."""
    if os.environ.get("PRINTER_HOST"):
        return os.environ["PRINTER_HOST"]
    for _, path in _preset_index("machine").items():
        if "system" in path.parts:
            continue
        host = json.loads(path.read_text()).get("print_host")
        if host:
            return host
    return None


async def _printer(host: str | None, control: bool = False):
    from pycentauri import connect_auto, discover
    h = host or _default_host()
    if not h:
        raise ValueError("No printer host given, no PRINTER_HOST env var, and "
                         "no print_host found in machine presets.")
    mainboard_id = None
    try:
        found = await discover(timeout=3.0)
        for d in found:
            if getattr(d, "host", None) == h:
                mainboard_id = getattr(d, "mainboard_id", None)
                break
    except Exception:
        pass  # discovery is best-effort; connect_auto works without it mid-print
    return await connect_auto(h, enable_control=control, mainboard_id=mainboard_id)


def _state_name(code: int) -> str:
    from pycentauri.models import PrintStatus
    for name in dir(PrintStatus):
        if not name.startswith("_") and getattr(PrintStatus, name) == code:
            return name.lower()
    return f"unknown({code})"


def _status_dict(s) -> dict:
    pi = s.print_info
    return {
        "state": _state_name(pi.status),
        "state_code": pi.status,
        "print": {
            "filename": pi.filename,
            "current_layer": pi.current_layer,
            "total_layers": pi.total_layer,
            "progress_pct": pi.progress,
            "elapsed_s": pi.current_ticks,
            "total_s": pi.total_ticks,
            "error_code": pi.err_num,
            "speed_pct": pi.print_speed,
            "task_id": pi.task_id,
        },
        "temps_c": {
            "nozzle": round(s.temp_nozzle, 1), "nozzle_target": s.temp_nozzle_target,
            "bed": round(s.temp_bed, 1), "bed_target": s.temp_bed_target,
            "chamber": round(s.temp_chamber, 1), "chamber_target": s.temp_chamber_target,
        },
        "fans_pct": dict(s.fan_speed) if s.fan_speed else {},
        "position": list(s.coord) if s.coord else None,
        "z_offset": s.z_offset,
    }


@mcp.tool()
async def printer_status(host: str | None = None) -> dict:
    """Live printer status: decoded state (idle/printing/auto_leveling/...),
    temps, layer progress, error code. host defaults to PRINTER_HOST or the
    print_host in your OrcaSlicer machine preset."""
    p = await _printer(host)
    try:
        return _status_dict(await p.status())
    finally:
        await p.close()


@mcp.tool()
async def printer_snapshot(host: str | None = None) -> Image:
    """Grab a webcam snapshot from the printer's camera — use it to check
    first-layer adhesion and mid-print health remotely."""
    p = await _printer(host)
    try:
        jpeg = await p.snapshot()
    finally:
        await p.close()
    return Image(data=jpeg, format="jpeg")


@mcp.tool()
async def printer_attributes(host: str | None = None) -> dict:
    """Printer identity and firmware info (model, firmware version,
    mainboard id) — useful when debugging protocol quirks."""
    p = await _printer(host)
    try:
        a = await p.attributes()
        return {k: v for k, v in a.model_dump().items() if k != "raw"}
    finally:
        await p.close()


@mcp.tool()
async def printer_files(host: str | None = None) -> dict:
    """List G-code files stored on the printer. NOTE: the CC1 firmware does not
    support this over SDCP (works on CC2); on CC1 manage files on the
    touchscreen."""
    p = await _printer(host)
    try:
        return await p.list_files()
    except Exception as e:
        return {"supported": False, "error": str(e)}
    finally:
        await p.close()


@mcp.tool()
async def upload_gcode(gcode_path: str, host: str | None = None,
                       remote_name: str | None = None) -> str:
    """Upload a G-code file to the printer. Does NOT start printing —
    use start_print for that."""
    src = Path(gcode_path).expanduser()
    if not src.is_file():
        raise ValueError(f"File not found: {gcode_path}")
    p = await _printer(host, control=True)
    try:
        name = await p.upload_file(str(src), remote_name=remote_name)
        return f"Uploaded as {name} ({src.stat().st_size} bytes)"
    except Exception as e:
        if "500" in str(e):
            raise RuntimeError(
                f"Upload failed: {e}. On the CC1 this usually means the "
                "printer is busy (printing/leveling/maintenance) or its "
                "storage is full — the firmware can't report which. Wait for "
                "idle and/or delete old files on the touchscreen, then retry."
            ) from e
        raise
    finally:
        await p.close()


_start_lock = None  # created lazily; module import happens outside a loop


@mcp.tool()
async def start_print(filename: str, host: str | None = None,
                      auto_leveling: bool = True,
                      plate_cleared: bool = False) -> dict:
    """Start printing a file already on the printer (see upload_gcode).
    Physical action — confirm with the user before calling. Verifies the
    printer was idle, issues the command, then polls until the printer
    actually enters its pre-print/printing sequence (the firmware silently
    drops start commands sent while it is busy).

    If the previous job COMPLETED or was STOPPED, the old part may still be
    on the plate and the toolhead would crash into it: you must ask the user
    to confirm the plate is empty, then pass plate_cleared=True."""
    import asyncio
    from pycentauri.models import PrintStatus
    global _start_lock
    if _start_lock is None:
        _start_lock = asyncio.Lock()
    ACTIVE = {PrintStatus.HOMING, PrintStatus.PREHEATING, PrintStatus.AUTO_LEVELING,
              PrintStatus.RESONANCE_TESTING, PrintStatus.PRINT_START,
              PrintStatus.PRINTING, PrintStatus.FILE_CHECKING,
              PrintStatus.PRINTER_CHECKING, PrintStatus.AUTO_LEVELING_COMPLETED,
              PrintStatus.PREHEATING_COMPLETED, PrintStatus.HOMING_COMPLETED,
              PrintStatus.RESONANCE_TESTING_COMPLETED}
    if _start_lock.locked():
        raise RuntimeError("Another start_print is already in progress.")
    async with _start_lock:
        p = await _printer(host, control=True)
        try:
            before = (await p.status()).print_info.status
            if before in (PrintStatus.COMPLETED, PrintStatus.STOPPED) and not plate_cleared:
                raise RuntimeError(
                    f"Previous job state is {_state_name(before)} — the old "
                    "part may still be on the plate and the toolhead would "
                    "crash into it. Confirm with the user that the plate is "
                    "empty, then call again with plate_cleared=True.")
            if before not in (PrintStatus.IDLE, PrintStatus.COMPLETED, PrintStatus.STOPPED):
                raise RuntimeError(
                    f"Printer is not idle (state: {_state_name(before)}) — the "
                    "firmware silently drops start commands while busy. Wait for "
                    "idle, then retry.")
            await p.start_print(filename, auto_leveling=auto_leveling)
            deadline = time.monotonic() + 30
            last = before
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                st = await p.status()
                last = st.print_info.status
                if last == PrintStatus.ERROR:
                    raise RuntimeError(
                        f"Printer entered ERROR after start (error_code="
                        f"{st.print_info.err_num}).")
                if last in ACTIVE:
                    return {"started": True, "filename": filename,
                            "state": _state_name(last),
                            "note": "Pre-print routine (clean/level/calibrate) "
                                    "takes ~10-15 min before extrusion begins."}
            raise RuntimeError(
                f"start_print was issued but the printer never left "
                f"{_state_name(last)} within 30s — the command was likely "
                "dropped. Check the printer and retry.")
        finally:
            await p.close()


@mcp.tool()
async def print_control(action: str, host: str | None = None) -> str:
    """Pause, resume, or stop the current print. action: pause|resume|stop.
    resume only works from a PAUSED state — resuming a stopped/errored job
    would drive the nozzle into whatever went wrong."""
    from pycentauri.models import PrintStatus
    if action not in ("pause", "resume", "stop"):
        raise ValueError("action must be pause, resume, or stop")
    p = await _printer(host, control=True)
    try:
        if action == "resume":
            state = (await p.status()).print_info.status
            if state not in (PrintStatus.PAUSED, PrintStatus.PAUSING):
                raise RuntimeError(
                    f"Refusing to resume from {_state_name(state)} — resume "
                    "is only safe from a paused print.")
        await getattr(p, action)()
        return f"OK: {action}"
    finally:
        await p.close()


if __name__ == "__main__":
    mcp.run()
