"""Self-check for the OrcaSlicer MCP server. Run: python test_server.py
Slicing tests need OrcaSlicer installed + at least one full preset set;
printer tests are skipped unless the printer is reachable."""

import json
import re
import tempfile
from pathlib import Path

import server


def test_gcode_stats():
    g = Path(tempfile.mkstemp(suffix=".gcode")[1])
    g.write_text(
        ";curr_bed_type:Textured PEI Plate\n"
        "M190 S65\nM109 S220\n"
        "; total layer number: 100\n"
        "; total filament used [g] = 5.04\n"
        "; estimated printing time (normal mode) = 15m 25s\n"
        "; printer_settings_id = Test Printer\n")
    s = server._gcode_stats(g)
    assert s["bed_temp"] == 65 and isinstance(s["bed_temp"], int), s
    assert s["nozzle_temp"] == 220, s
    assert s["layer_count"] == 100 and isinstance(s["layer_count"], int), s
    assert s["filament_used_g"] == 5.04, s
    assert s["bed_type"] == "Textured PEI Plate", s
    g.unlink()


def test_state_names():
    assert server._state_name(13) == "printing"
    assert server._state_name(0) == "idle"
    assert server._state_name(14) == "error"
    assert "unknown" in server._state_name(999)


def test_preset_resolution():
    idx = server._preset_index("machine")
    if not idx:
        print("  (skipped: no OrcaSlicer presets on this machine)")
        return
    name = next(iter(idx))
    merged = server._resolve(name, "machine", idx)
    assert merged.get("name") == name
    assert server._system_ancestor(name, idx)


def test_slice_end_to_end():
    if not Path(server.ORCA_BIN).exists():
        print("  (skipped: OrcaSlicer not installed)")
        return
    machines = server._preset_index("machine")
    target = next((n for n in sorted(machines)
                   if "Centauri Carbon 0.4" in n), None)
    if not target:
        print("  (skipped: no Centauri Carbon 0.4 preset)")
        return
    cube = Path(__file__).parent / "test/cube.stl"
    procs = server.list_profiles("process", search="0.20mm Standard @Elegoo CC")
    fils = server.list_profiles("filament", search="Generic PLA @System")
    if not (cube.exists() and procs["process"] and fils["filament"]):
        print("  (skipped: fixtures/presets missing)")
        return
    with tempfile.TemporaryDirectory() as out:
        r = server.slice_model(str(cube), printer=target,
                               process=procs["process"][0]["name"],
                               filament=fils["filament"][0]["name"],
                               bed_type="Textured PEI Plate", output_dir=out)
        p = r["plates"][0]
        assert p["bed_temp"] > 0 and p["layer_count"] > 0, p
        assert isinstance(p["bed_temp"], (int, float)), p
        g = Path(p["file"]).read_text(errors="replace")
        # the ;curr_bed_type comment is machine-template-dependent; the M190
        # command is what the machine executes — assert on that
        assert re.search(r"M190 S\d", g)


if __name__ == "__main__":
    for fn in [test_gcode_stats, test_state_names, test_preset_resolution,
               test_slice_end_to_end]:
        print(f"{fn.__name__} ...")
        fn()
    print("ALL OK")
