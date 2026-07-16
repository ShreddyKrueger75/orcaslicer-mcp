"""OrcaSlicer MCP server.

Tools: profile listing/reading/editing, headless slicing via the OrcaSlicer CLI,
G-code analysis, and Elegoo Centauri Carbon printer control (via pycentauri).
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ORCA_BIN = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
ORCA_DATA = Path.home() / "Library/Application Support/OrcaSlicer"
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
        raise ValueError(f"{kind} preset not found: {name!r}. "
                         f"Known: {sorted(idx)[:20]}")
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
def list_profiles(kind: str | None = None) -> dict:
    """List OrcaSlicer presets. kind: machine | process | filament | None (all).
    Returns names with their source (system or user)."""
    kinds = [kind] if kind else list(KINDS)
    out = {}
    for k in kinds:
        if k not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        out[k] = [
            {"name": n, "source": "system" if "system" in p.parts else "user"}
            for n, p in sorted(_preset_index(k).items())
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
    d = json.loads(path.read_text())
    d.update(settings)
    path.write_text(json.dumps(d, indent=4))
    return f"Updated {path} ({len(settings)} key(s))"


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


def _gcode_stats(path: Path) -> dict:
    """Pull the useful metadata Orca writes into G-code comments."""
    text = path.read_text(errors="replace")
    stats: dict = {"file": str(path), "size_bytes": path.stat().st_size}
    patterns = {
        "estimated_time": r"; (?:model printing time|estimated printing time \(normal mode\))\s*[=:]\s*(.+)",
        "total_time": r"; total estimated time\s*[=:]\s*(.+)",
        "filament_used_g": r"; total filament used \[g\]\s*=\s*([\d.]+)",
        "filament_used_mm": r"; filament used \[mm\]\s*=\s*([\d.]+)",
        "filament_cost": r"; total filament cost\s*=\s*([\d.]+)",
        "layer_count": r"; total layer number:\s*(\d+)",
        "max_z_height": r"; max_z_height:\s*([\d.]+)",
        "nozzle_temp": r"; nozzle_temperature = (.+)",
        "bed_temp": r"; (?:hot_plate|textured_plate|cool_plate|eng_plate)_temp = (.+)",
        "filament_type": r"; filament_type = (.+)",
        "printer": r"; printer_settings_id = (.+)",
        "print_profile": r"; print_settings_id = (.+)",
        "filament_profile": r"; filament_settings_id = (.+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            stats[key] = m.group(1).strip()
    return stats


@mcp.tool()
def slice_model(
    model_path: str,
    printer: str,
    process: str,
    filament: str,
    output_dir: str | None = None,
    scale: float | None = None,
    rotate_z: float | None = None,
    arrange: bool = True,
    orient: bool = False,
) -> dict:
    """Slice an STL/3MF/STEP file to G-code headlessly using named OrcaSlicer
    presets (see list_profiles). Returns G-code path(s) plus print time and
    filament estimates. orient=True lets Orca pick the orientation — leave off
    if the model is already oriented correctly."""
    model = Path(model_path).expanduser()
    if not model.is_file():
        raise ValueError(f"Model not found: {model}")
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
        return {"gcode_files": [str(g) for g in gcodes],
                "plates": [_gcode_stats(g) for g in gcodes]}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@mcp.tool()
def analyze_gcode(gcode_path: str) -> dict:
    """Parse an Orca-sliced G-code file: print time, filament use, layer count,
    temperatures, and profile names."""
    p = Path(gcode_path).expanduser()
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    return _gcode_stats(p)


# ---------------------------------------------------------------- printer


def _default_host() -> str | None:
    """First print_host found in user machine presets."""
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
        raise ValueError("No printer host given and none found in machine presets.")
    mainboard_id = None
    try:
        found = await discover(timeout=3.0)
        for d in found:
            if getattr(d, "ip", None) == h or getattr(d, "host", None) == h:
                mainboard_id = getattr(d, "mainboard_id", None)
                break
    except Exception:
        pass  # discovery is best-effort; connect_auto works without it mid-print
    return await connect_auto(h, enable_control=control, mainboard_id=mainboard_id)


@mcp.tool()
async def printer_status(host: str | None = None) -> dict:
    """Live printer status: temps, print progress, current job. host defaults
    to the print_host in your OrcaSlicer machine preset."""
    p = await _printer(host)
    try:
        s = await p.status()
        return {k: str(v) for k, v in vars(s).items()
                if not k.startswith("_") and k != "raw"}
    finally:
        await p.close()


@mcp.tool()
async def printer_files(host: str | None = None) -> dict:
    """List G-code files stored on the printer."""
    p = await _printer(host)
    try:
        return await p.list_files()
    finally:
        await p.close()


@mcp.tool()
async def upload_gcode(gcode_path: str, host: str | None = None,
                       remote_name: str | None = None) -> str:
    """Upload a G-code file to the printer. Does NOT start printing —
    use start_print for that."""
    if not Path(gcode_path).expanduser().is_file():
        raise ValueError(f"File not found: {gcode_path}")
    p = await _printer(host, control=True)
    try:
        name = await p.upload_file(str(Path(gcode_path).expanduser()),
                                   remote_name=remote_name)
        return f"Uploaded as {name}"
    finally:
        await p.close()


@mcp.tool()
async def start_print(filename: str, host: str | None = None,
                      auto_leveling: bool = True) -> str:
    """Start printing a file already on the printer (see printer_files /
    upload_gcode). Physical action — confirm with the user before calling."""
    p = await _printer(host, control=True)
    try:
        await p.start_print(filename, auto_leveling=auto_leveling)
        return f"Print started: {filename}"
    finally:
        await p.close()


@mcp.tool()
async def print_control(action: str, host: str | None = None) -> str:
    """Pause, resume, or stop the current print. action: pause|resume|stop."""
    if action not in ("pause", "resume", "stop"):
        raise ValueError("action must be pause, resume, or stop")
    p = await _printer(host, control=True)
    try:
        await getattr(p, action)()
        return f"OK: {action}"
    finally:
        await p.close()


if __name__ == "__main__":
    mcp.run()
