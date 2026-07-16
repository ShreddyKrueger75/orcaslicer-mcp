"""Self-check for the OrcaSlicer MCP server. Run: python test_server.py

Slicing tests need OrcaSlicer installed + a full preset set. Backend tests run
against mock HTTP transports — no printer required — and assert the safety
rules that matter: upload must never start a print, and the status mapping must
be right for every protocol.
"""

import asyncio
import json
import re
import tempfile
from pathlib import Path

import httpx

import backends
import server
from backends import Target


# ---------------------------------------------------------------- slicing


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


def test_bed_types_match_orcaslicer():
    # Every plate Orca knows must be accepted, or we lock users out of their
    # real hardware (Supertack = Bambu A1/H2, Textured Cool = A1 mini).
    for plate in ("Cool Plate", "Supertack Plate", "Textured Cool Plate",
                  "Engineering Plate", "High Temp Plate", "Textured PEI Plate",
                  "Default Plate"):
        assert plate in server.VALID_BED_TYPES, plate


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
    cube = Path(__file__).parent / "test/cube.stl"
    procs = server.list_profiles("process", search="0.20mm Standard @Elegoo CC")
    fils = server.list_profiles("filament", search="Generic PLA @System")
    if not (target and cube.exists() and procs["process"] and fils["filament"]):
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
        # the machine executes M190 — assert on the command, not a comment
        assert re.search(r"M190 S\d", g)


# ---------------------------------------------------------------- backends


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _install(backend, handler):
    """Give a backend a mock-transport client instead of a real one."""
    backend._client = httpx.AsyncClient(base_url=backend.base,
                                        headers=backend._headers(),
                                        transport=_mock(handler))
    return backend


def test_moonraker_status():
    body = {"result": {"status": {
        "print_stats": {"state": "printing", "filename": "cube.gcode",
                        "info": {"current_layer": 7, "total_layer": 100}},
        "extruder": {"temperature": 219.4, "target": 220},
        "heater_bed": {"temperature": 64.9, "target": 65},
        "virtual_sdcard": {"progress": 0.07}}}}
    b = _install(backends.MoonrakerBackend(Target("moonraker", "h")),
                 lambda r: httpx.Response(200, json=body))
    s = asyncio.run(b.status())
    assert s["state"] == backends.PRINTING, s
    assert s["print"]["current_layer"] == 7 and s["print"]["total_layers"] == 100
    assert s["print"]["progress_pct"] == 7, s
    assert s["temps_c"]["bed"] == 64.9 and s["temps_c"]["nozzle_target"] == 220


def test_moonraker_upload_does_not_print():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(201, json={"item": {}, "print_started": False})

    f = Path(tempfile.mkstemp(suffix=".gcode")[1])
    f.write_text("G28\n")
    b = _install(backends.MoonrakerBackend(Target("moonraker", "h")), handler)
    asyncio.run(b.upload(f, "x.gcode"))
    # `print` must never be sent: Moonraker starts the job when it is "true"
    assert b'name="print"' not in seen["body"], "upload must not send print flag"
    assert b'name="root"' in seen["body"]
    f.unlink()


def test_octoprint_upload_does_not_print():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(201, json={"done": True})

    f = Path(tempfile.mkstemp(suffix=".gcode")[1])
    f.write_text("G28\n")
    b = _install(backends.OctoPrintBackend(Target("octoprint", "h", api_key="k")),
                 handler)
    asyncio.run(b.upload(f, "x.gcode"))
    for flag in (b'name="print"', b'name="select"'):
        assert flag not in seen["body"], f"upload must not send {flag!r}"
    f.unlink()


def test_octoprint_status_flags():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/printer":
            return httpx.Response(200, json={
                "state": {"text": "Paused", "flags": {"paused": True,
                                                      "operational": True}},
                "temperature": {"tool0": {"actual": 200.1, "target": 0},
                                "bed": {"actual": 60.0, "target": 60}}})
        return httpx.Response(200, json={"job": {"file": {"name": "a.gcode"}},
                                         "progress": {"completion": 42.5}})

    b = _install(backends.OctoPrintBackend(Target("octoprint", "h", api_key="k")),
                 handler)
    s = asyncio.run(b.status())
    assert s["state"] == backends.PAUSED, s          # paused wins over operational
    assert s["print"]["progress_pct"] == 42, s
    assert s["print"]["filename"] == "a.gcode"


def test_prusalink_status_and_upload_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen["headers"] = request.headers
            return httpx.Response(201, json={})
        return httpx.Response(200, json={
            "printer": {"state": "PRINTING", "temp_nozzle": 214.9,
                        "target_nozzle": 215.0, "temp_bed": 59.5,
                        "target_bed": 60.0},
            "job": {"id": 420, "progress": 42.0, "time_remaining": 520,
                    "time_printing": 100}})

    b = _install(backends.PrusaLinkBackend(Target("prusalink", "h", password="p")),
                 handler)
    s = asyncio.run(b.status())
    assert s["state"] == backends.PRINTING, s
    assert s["print"]["progress_pct"] == 42, s   # already a percent, not 0-1
    assert s["temps_c"]["nozzle"] == 214.9

    f = Path(tempfile.mkstemp(suffix=".gcode")[1])
    f.write_text("G28\n")
    asyncio.run(b.upload(f, "x.gcode"))
    assert seen["headers"]["Print-After-Upload"] == "?0", "must not auto-print"
    f.unlink()


def test_duet_status_and_err_field():
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/rr_connect":
            return httpx.Response(200, json={"err": 0, "sessionKey": 123})
        key = request.url.params.get("key")
        return httpx.Response(200, json={"result": {
            "state": {"status": "processing"},
            "heat": {"bedHeaters": [0], "heaters": [
                {"current": 60.1, "active": 60},
                {"current": 210.2, "active": 210}]},
            "job": {"layer": 5, "filePosition": 500,
                    "file": {"fileName": "a.gcode", "size": 1000,
                             "numLayers": 50}}}[key]})

    b = _install(backends.DuetBackend(Target("duet", "h")), handler)
    s = asyncio.run(b.status())
    assert s["state"] == backends.PRINTING, s     # "processing" means printing
    assert s["temps_c"]["nozzle"] == 210.2 and s["temps_c"]["bed"] == 60.1
    assert s["print"]["progress_pct"] == 50, s
    assert s["print"]["current_layer"] == 5


def test_duet_bad_password_raises():
    b = _install(backends.DuetBackend(Target("duet", "h", password="wrong")),
                 lambda r: httpx.Response(200, json={"err": 1}))
    try:
        asyncio.run(b.status())
    except RuntimeError as e:
        assert "password" in str(e)
        return
    raise AssertionError("bad password must raise")


def test_orca_preset_mapping():
    t = backends.from_orca_preset(
        {"print_host": "192.168.1.9", "host_type": "moonraker",
         "printhost_apikey": "abc"}, "My Voron")
    assert t.type == "moonraker" and t.host == "192.168.1.9" and t.api_key == "abc"
    # cloud host types have no local API — must be reported, not silently used
    try:
        backends.from_orca_preset({"print_host": "x", "host_type": "prusaconnect"},
                                  "P")
    except backends.Unsupported as e:
        assert "PrusaLink" in str(e)
    else:
        raise AssertionError("prusaconnect must raise Unsupported")
    assert backends.from_orca_preset({"host_type": "octoprint"}, "no host") is None


def test_state_vocabulary_is_shared():
    # every backend must map onto the same vocabulary the tools gate on
    for cls in backends.BACKENDS.values():
        for table in (getattr(cls, "_STATE", {}),):
            for v in table.values():
                assert v in {backends.IDLE, backends.HEATING, backends.PRINTING,
                             backends.PAUSED, backends.COMPLETE,
                             backends.STOPPED, backends.ERROR, backends.BUSY}, v


def test_unconfigured_error_tells_client_to_ask():
    saved = (server.backends.from_env, server.backends.load_config,
             server._orca_target)
    server.backends.from_env = lambda: None
    server.backends.load_config = lambda: None
    server._orca_target = lambda: None
    try:
        server._resolve_target()
    except backends.NotConfigured as e:
        assert "printer_setup" in str(e) and "ASK THE USER" in str(e)
    else:
        raise AssertionError("must raise NotConfigured")
    finally:
        (server.backends.from_env, server.backends.load_config,
         server._orca_target) = saved


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"{fn.__name__} ...")
        fn()
    print(f"ALL OK ({len(tests)} tests)")
