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


VOCAB = {backends.IDLE, backends.HEATING, backends.PRINTING, backends.PAUSED,
         backends.COMPLETE, backends.STOPPED, backends.ERROR, backends.BUSY}


def test_state_vocabulary_is_shared():
    # every backend must map onto the vocabulary the safety gates key off.
    # Elegoo maps in a method, not a table — check it for real, or this test
    # silently covers nothing.
    checked = 0
    for cls in backends.BACKENDS.values():
        for v in getattr(cls, "_STATE", {}).values():
            assert v in VOCAB, v
            checked += 1
    from pycentauri.models import PrintStatus
    codes = [getattr(PrintStatus, n) for n in dir(PrintStatus)
             if not n.startswith("_")]
    for code in codes:
        v = backends.ElegooBackend._map(code)
        assert v in VOCAB, (code, v)
        checked += 1
    assert checked > 40, f"only {checked} mappings checked"


def test_elegoo_finished_job_trips_plate_gate():
    from pycentauri.models import PrintStatus
    assert backends.ElegooBackend._map(PrintStatus.COMPLETED) in \
        backends.PLATE_DIRTY_STATES
    assert backends.ElegooBackend._map(PrintStatus.STOPPED) in \
        backends.PLATE_DIRTY_STATES
    # the long pre-print routine must count as "started", not as "dropped"
    for s in (PrintStatus.AUTO_LEVELING, PrintStatus.HOMING,
              PrintStatus.PREHEATING, PrintStatus.RESONANCE_TESTING):
        assert backends.ElegooBackend._map(s) in backends.ACTIVE_STATES


def test_safe_name_blocks_gcode_injection():
    # a filename is data, never syntax: on RepRapFirmware `;` separates
    # commands, so this would have set the nozzle to 300C
    for evil in ['a.gcode"; M109 S300 ; "', "a;M104 S300", '../../etc/passwd',
                 "/abs/path.gcode", "a\nM109 S300", 'a`whoami`.gcode', ""]:
        try:
            backends.safe_name(evil)
        except ValueError:
            continue
        raise AssertionError(f"safe_name accepted {evil!r}")
    # real slicer output must still pass (spaces, dots, dashes, subfolders)
    for ok in ["ECC_0.4_Jazzmaster_part2_Elegoo PLA-CF _0.08_1d18h8m.gcode",
               "plate_1.gcode", "subdir/part (v2).3mf", "a.gcode.3mf"]:
        assert backends.safe_name(ok) == ok


def test_duet_start_rejects_injection():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"err": 0, "sessionKey": 1})

    b = _install(backends.DuetBackend(Target("duet", "h")), handler)
    try:
        asyncio.run(b.start('x.gcode"; M109 S300 ; "'))
    except ValueError:
        assert not any("M109" in c for c in calls), "injected gcode was sent!"
        return
    raise AssertionError("Duet start must reject an injecting filename")


def test_duet_reuses_one_session():
    # RRF has a small session pool and only rr_disconnect frees ours;
    # reconnecting per call used to exhaust it after ~8 calls
    connects, disconnects = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/rr_connect":
            connects.append(1)
            return httpx.Response(200, json={"err": 0, "sessionKey": 7})
        if p == "/rr_disconnect":
            disconnects.append(1)
            return httpx.Response(200, json={"err": 0})
        return httpx.Response(200, json={"result": {}})

    b = _install(backends.DuetBackend(Target("duet", "h")), handler)

    async def go():
        for _ in range(5):
            await b._connect()
        await b.close()

    asyncio.run(go())
    assert len(connects) == 1, f"{len(connects)} sessions opened, want 1"
    assert len(disconnects) == 1, "session never released"


def test_duet_finished_print_trips_plate_gate():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rr_connect":
            return httpx.Response(200, json={"err": 0, "sessionKey": 1})
        key = request.url.params.get("key")
        return httpx.Response(200, json={"result": {
            "state": {"status": "idle"},
            "heat": {"bedHeaters": [0], "heaters": [{"current": 25, "active": 0},
                                                    {"current": 25, "active": 0}]},
            # RRF says plain "idle" after a print; lastFileName is the tell
            "job": {"lastFileName": "done.gcode"}}[key]})

    b = _install(backends.DuetBackend(Target("duet", "h")), handler)
    s = asyncio.run(b.status())
    assert s["state"] in backends.PLATE_DIRTY_STATES, s
    assert s["state"] == backends.COMPLETE


def test_duet_no_bed_does_not_report_hotend_as_bed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rr_connect":
            return httpx.Response(200, json={"err": 0, "sessionKey": 1})
        key = request.url.params.get("key")
        return httpx.Response(200, json={"result": {
            "state": {"status": "idle"},
            # bedHeaters [-1] = no bed; heaters[-1] would be the hotend
            "heat": {"bedHeaters": [-1], "heaters": [{"current": 245.0,
                                                      "active": 245}]},
            "job": {}}[key]})

    b = _install(backends.DuetBackend(Target("duet", "h")), handler)
    s = asyncio.run(b.status())
    assert s["temps_c"]["bed"] is None, f"reported {s['temps_c']['bed']}C bed"


def test_octoprint_finished_print_trips_plate_gate():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/printer":
            return httpx.Response(200, json={
                "state": {"text": "Operational", "flags": {"operational": True}},
                "temperature": {"tool0": {"actual": 30, "target": 0},
                                "bed": {"actual": 25, "target": 0}}})
        return httpx.Response(200, json={"job": {"file": {"name": "a.gcode"}},
                                         "progress": {"completion": 100.0}})

    b = _install(backends.OctoPrintBackend(Target("octoprint", "h", api_key="k")),
                 handler)
    s = asyncio.run(b.status())
    assert s["state"] == backends.COMPLETE, s


def test_octoprint_disconnected_reports_error():
    b = _install(backends.OctoPrintBackend(Target("octoprint", "h", api_key="k")),
                 lambda r: httpx.Response(409, json={"error": "not operational"}))
    s = asyncio.run(b.status())
    assert s["state"] == backends.ERROR and "409" in str(s["print"]["error_code"])


def test_moonraker_klippy_down_says_so():
    b = _install(backends.MoonrakerBackend(Target("moonraker", "h")),
                 lambda r: httpx.Response(200, json={"result": None}))
    try:
        asyncio.run(b.status())
    except RuntimeError as e:
        assert "Klippy" in str(e), e
        return
    raise AssertionError("null result must raise a readable error")


def test_moonraker_null_temperature_does_not_crash():
    body = {"result": {"status": {
        "print_stats": {"state": "printing", "filename": "a.gcode"},
        "extruder": {"temperature": None, "target": 205},
        "heater_bed": {}, "virtual_sdcard": {"progress": None}}}}
    b = _install(backends.MoonrakerBackend(Target("moonraker", "h")),
                 lambda r: httpx.Response(200, json=body))
    s = asyncio.run(b.status())
    assert s["state"] == backends.PRINTING and s["temps_c"]["nozzle"] is None


def test_config_file_garbage_is_a_friendly_error():
    for bad in ({"host": "1.2.3.4"}, {"type": "moonraker"},
                {"type": "moonraker", "host": "h", "bogus": 1}, {}):
        try:
            Target.from_dict(bad, "test")
        except backends.NotConfigured as e:
            assert "configure_printer" in str(e)
            continue
        raise AssertionError(f"accepted {bad}")
    t = Target.from_dict({"type": "elegoo", "host": "h"}, "test")
    assert t.type == "elegoo"


def test_unknown_host_is_refused_not_guessed():
    saved = (server.backends.from_env, server.backends.load_config,
             server._orca_targets)
    server.backends.from_env = lambda: Target("octoprint", "known.local",
                                              api_key="secret")
    server.backends.load_config = lambda: None
    server._orca_targets = lambda: []
    try:
        server._resolve_target(host="192.0.2.77")   # a different machine
    except backends.NotConfigured as e:
        assert "protocol and credentials are unknown" in str(e)
    else:
        raise AssertionError("must not reuse another printer's protocol/creds")
    finally:
        (server.backends.from_env, server.backends.load_config,
         server._orca_targets) = saved


def test_multiple_orca_printers_refuses_to_guess():
    saved = (server._orca_targets, server._gui_project)
    server._orca_targets = lambda: [Target("elegoo", "10.0.0.1", source="a"),
                                    Target("moonraker", "10.0.0.2", source="b")]
    server._gui_project = lambda: None
    try:
        server._orca_target()
    except backends.NotConfigured as e:
        assert "ASK THE USER" in str(e) and "10.0.0.2" in str(e)
    else:
        raise AssertionError("must not silently pick one of two printers")
    finally:
        server._orca_targets, server._gui_project = saved


def test_broken_preset_does_not_blind_every_tool():
    # a single cloud-only (PrusaConnect) preset used to raise Unsupported out
    # of the resolution loop and break every printer tool
    saved = server._preset_index
    calls = {}

    def fake_index(kind):
        return calls.get(kind, {})

    try:
        server._preset_index = fake_index
        assert server._orca_targets() == []
    finally:
        server._preset_index = saved


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


# -------------------------------------------------- slicing depth (WS2)


def test_int_list_validation():
    assert server._int_list("1,3,5", "x") == "1,3,5"
    assert server._int_list(" 1 , 2 ", "x") == "1,2"
    # negatives/zero are not valid 1-based indices — must be rejected
    for bad in ("a,b", "1,x", "", "1;2", "-1", "-1,3", "--5", "0", "1,0"):
        try:
            server._int_list(bad, "skip_objects")
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_slice_filament_ids_bounds_checked():
    # referencing more slots than filaments given must fail early, clearly
    machines = server._preset_index("machine")
    fils = server.list_profiles("filament")["filament"]
    procs = server._preset_index("process")
    if not (machines and len(fils) >= 1 and procs):
        print("  (skipped: no OrcaSlicer presets)")
        return
    try:
        _spy_slice_argv(model_path="test/cube.stl", printer=next(iter(machines)),
                        process=next(iter(procs)), filaments=[fils[0]["name"]],
                        filament_ids="1,2", bed_type="Textured PEI Plate")
    except ValueError as e:
        assert "slot 2" in str(e) and "1 filament" in str(e)
        return
    raise AssertionError("filament_ids beyond the filament count must raise")


def _spy_slice_argv(**kwargs):
    """Run slice_model but capture the CLI argv instead of really slicing."""
    import subprocess
    orig, seen = subprocess.run, {}

    class _Done:
        returncode = 0
        stdout = stderr = ""

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        # write a fake plate so the no-gcode guard passes
        out = cmd[cmd.index("--outputdir") + 1]
        Path(out, "plate_1.gcode").write_text("M190 S65\nM109 S220\n"
                                              "; total layer number: 1\n")
        return _Done()

    subprocess.run = spy
    try:
        with tempfile.TemporaryDirectory() as out:
            server.slice_model(output_dir=out, **kwargs)
        return seen["cmd"]
    finally:
        subprocess.run = orig


def test_slice_multi_filament_builds_argv():
    machines = server._preset_index("machine")
    fils = server.list_profiles("filament")["filament"]
    procs = server._preset_index("process")
    if not (machines and len(fils) >= 2 and procs):
        print("  (skipped: no OrcaSlicer presets)")
        return
    printer = next(iter(machines))
    proc = next(iter(procs))
    f1, f2 = fils[0]["name"], fils[1]["name"]
    cmd = _spy_slice_argv(model_path="test/cube.stl", printer=printer,
                          process=proc, filaments=[f1, f2], filament_ids="1,2",
                          plate=2, skip_objects="3,5",
                          bed_type="Textured PEI Plate")
    lf = cmd[cmd.index("--load-filaments") + 1]
    assert lf.count(";") == 1, "two filaments must be joined with ;"
    assert cmd[cmd.index("--slice") + 1] == "2"
    assert cmd[cmd.index("--load-filament-ids") + 1] == "1,2"
    assert cmd[cmd.index("--skip-objects") + 1] == "3,5"


def test_slice_single_filament_backcompat():
    machines = server._preset_index("machine")
    fils = server.list_profiles("filament")["filament"]
    procs = server._preset_index("process")
    if not (machines and fils and procs):
        print("  (skipped: no OrcaSlicer presets)")
        return
    cmd = _spy_slice_argv(model_path="test/cube.stl", printer=next(iter(machines)),
                          process=next(iter(procs)), filament=fils[0]["name"],
                          bed_type="Textured PEI Plate")
    assert ";" not in cmd[cmd.index("--load-filaments") + 1]
    assert cmd[cmd.index("--slice") + 1] == "0"        # all plates by default
    assert "--skip-objects" not in cmd


def test_slice_return_keeps_singular_filament_key():
    # single-material callers that predate multi-filament still read `filament`
    machines = server._preset_index("machine")
    fils = server.list_profiles("filament")["filament"]
    procs = server._preset_index("process")
    if not (machines and fils and procs):
        print("  (skipped: no OrcaSlicer presets)")
        return
    import subprocess
    orig, captured = subprocess.run, {}

    class _Done:
        returncode = 0
        stdout = stderr = ""

    def spy(cmd, **kw):
        out = cmd[cmd.index("--outputdir") + 1]
        Path(out, "plate_1.gcode").write_text("M190 S65\nM109 S220\n")
        return _Done()

    subprocess.run = spy
    try:
        with tempfile.TemporaryDirectory() as out:
            r = server.slice_model(model_path="test/cube.stl",
                                   printer=next(iter(machines)),
                                   process=next(iter(procs)),
                                   filament=fils[0]["name"],
                                   bed_type="Textured PEI Plate", output_dir=out)
    finally:
        subprocess.run = orig
    pu = r["presets_used"]
    assert pu["filament"] == fils[0]["name"]        # back-compat key present
    assert pu["filaments"] == [fils[0]["name"]]     # and the new list


# -------------------------------------------------- watch_print (WS1)


class _ScriptedBackend:
    """A fake backend that returns a scripted sequence of statuses."""

    name = "fake"

    def __init__(self, states):
        self._states = list(states)
        self._i = 0

    async def status(self):
        s = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        state, layer = (s if isinstance(s, tuple) else (s, 0))
        return {"state": state, "native_state": state,
                "print": {"progress_pct": 0, "current_layer": layer,
                          "total_layers": 100, "filename": "x.gcode",
                          "error_code": None},
                "temps_c": {"nozzle": 200.0, "bed": 60.0}}

    async def close(self):
        pass


def _run_watch(states, **kw):
    saved = server._backend

    async def fake_backend(host=None):
        return _ScriptedBackend(states)

    server._backend = fake_backend
    try:
        import asyncio
        return asyncio.run(server.watch_print(**kw))
    finally:
        server._backend = saved


def test_watch_print_returns_on_state_change():
    # heating -> heating -> printing: must return "changed" when it flips
    r = _run_watch([("heating", 0), ("heating", 0), ("printing", 1)],
                   until="change", timeout_s=60)
    assert r["reason"] == "changed" and r["state"] == "printing", r


def test_watch_print_returns_on_target_state():
    r = _run_watch([("printing", 5), ("printing", 6), ("complete", 100)],
                   until="complete", timeout_s=60)
    assert r["reason"] == "reached" and r["state"] == "complete", r
    assert r["layers_advanced"] == 95, r


def test_watch_print_error_returns_immediately():
    # even watching for "complete", an ERROR must short-circuit
    r = _run_watch([("error", 3)], until="complete", timeout_s=60)
    assert r["reason"] == "error" and r["state"] == "error", r


def test_watch_print_times_out_bounded():
    # steady state, never changes -> must return "timeout", not hang
    r = _run_watch([("printing", 7)], until="complete", timeout_s=3)
    assert r["reason"] == "timeout" and r["state"] == "printing", r


def test_watch_print_rejects_bad_until():
    try:
        _run_watch([("idle", 0)], until="frobnicate")
    except ValueError:
        return
    raise AssertionError("must reject an unknown 'until' state")


# -------------------------------------------------- end-to-end (WS3)
# real httpx client -> real socket -> fixture server. Proves the flows
# MockTransport can't: digest auth, session lifecycle, multipart over the wire.

import test_fixtures as fx


def _e2e(handler, ttype, **target_kw):
    with fx.fixture(handler) as port:
        t = Target(type=ttype, host="127.0.0.1", port=port, **target_kw)
        b = backends.make(t)
        f = Path(tempfile.mkstemp(suffix=".gcode")[1])
        f.write_text("G28\n")
        try:
            async def go():
                s = await b.status()
                await b.upload(f, "e2e.gcode")   # must not start a print
                await b.close()
                return s
            return asyncio.run(go()), handler.calls
        finally:
            f.unlink()


def test_e2e_moonraker():
    s, calls = _e2e(fx.MoonrakerHandler, "moonraker")
    assert s["state"] == backends.PRINTING and s["print"]["current_layer"] == 3
    assert ("POST", "/server/files/upload") in calls


def test_e2e_octoprint_api_key():
    s, calls = _e2e(fx.OctoPrintHandler, "octoprint", api_key="testkey")
    assert s["state"] == backends.PRINTING, s
    assert ("POST", "/api/files/local") in calls


def test_e2e_prusalink_digest_auth():
    # the whole point: httpx must complete the 401 Digest challenge over a real
    # socket. If digest were broken, status() would raise on the 401.
    s, calls = _e2e(fx.PrusaLinkHandler, "prusalink", user="maker", password="pw")
    assert s["state"] == backends.PRINTING, s
    # a 401 challenge then a re-request means the status path was hit twice
    assert calls.count(("GET", "/api/v1/status")) >= 2, calls
    assert ("PUT", "/api/v1/files/usb/e2e.gcode") in calls


def test_e2e_duet_session_lifecycle():
    fx.DuetHandler.sessions = 0
    s, calls = _e2e(fx.DuetHandler, "duet", password="reprap")
    assert s["state"] == backends.PRINTING, s
    # backend must have released the session it opened
    assert fx.DuetHandler.sessions == 0, "Duet session was not disconnected"
    assert ("GET", "/rr_disconnect") in calls
    # and reused one session across the multiple rr_model reads in status()
    assert calls.count(("GET", "/rr_connect")) == 1, calls


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"{fn.__name__} ...")
        fn()
    print(f"ALL OK ({len(tests)} tests)")
