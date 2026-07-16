"""Printer backends — one small adapter per network protocol.

Every backend normalizes to the same status shape and the same state names, so
the MCP tools never care what brand of printer is on the other end.

Protocols and the printers they reach:
  moonraker   Klipper machines (Voron, RatRig, Sovol, Creality K1, Neptune 4...)
  octoprint   anything behind OctoPrint (most Marlin printers)
  prusalink   Prusa MK4 / MK3.9 / XL / MINI / CORE One
  duet        Duet 2/3 boards running RepRapFirmware
  elegoo      Elegoo Centauri Carbon (SDCP), via pycentauri
  bambu       Bambu Lab P1/X1/A1/H2 in LAN mode, via the optional
              bambulabs-api extra

Endpoint/auth details were verified against each project's official docs and
source (see README "Printer support"). Where a protocol cannot do something
(no camera, no file listing), the backend declares it via capabilities() and
the tool reports "not supported by your printer" instead of a traceback.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

# ---- normalized state vocabulary (every backend maps onto these) ----------
IDLE = "idle"
HEATING = "heating"
PRINTING = "printing"
PAUSED = "paused"
COMPLETE = "complete"
STOPPED = "stopped"
ERROR = "error"
BUSY = "busy"

#: printing is demonstrably under way (used to verify a start command took)
ACTIVE_STATES = {HEATING, PRINTING, BUSY}
#: safe to start a new job from
STARTABLE_STATES = {IDLE, COMPLETE, STOPPED}
#: the old part is probably still sitting on the plate
PLATE_DIRTY_STATES = {COMPLETE, STOPPED}

TYPES = ("moonraker", "octoprint", "prusalink", "duet", "elegoo", "bambu")

#: OrcaSlicer's own host_type values -> our backend names. Orca stores the
#: printer's protocol in the machine preset, so an Orca user is already
#: configured; the rest of Orca's enum has no local API we can drive.
ORCA_HOST_TYPE = {
    "moonraker": "moonraker",
    "octoprint": "octoprint",
    "prusalink": "prusalink",
    "duet": "duet",
    "elegoolink": "elegoo",
}
ORCA_HOST_TYPE_UNSUPPORTED = {
    "prusaconnect": "PrusaConnect is Prusa's cloud; use PrusaLink (the "
                    "printer's own IP) instead.",
    "crealityprint": "CrealityPrint's cloud API isn't supported. If the "
                     "printer runs Klipper, use moonraker.",
    "obico": "Obico is a cloud relay; point this at the underlying "
             "OctoPrint/Klipper host instead.",
    "simplyprint": "SimplyPrint is a cloud service; use the local host.",
    "3dprinteros": "3DPrinterOS is a cloud service; use the local host.",
    "flashair": "FlashAir SD cards can't report status.",
    "astrobox": "AstroBox isn't supported.",
    "repetier": "Repetier Server isn't supported yet.",
    "mks": "MKS boards aren't supported yet.",
    "esp3d": "ESP3D isn't supported yet.",
    "flashforge": "FlashForge's protocol isn't supported yet.",
}


#: Filenames reach printer firmware as G-code arguments (Duet: M32 "name") and
#: as URL paths. A `;` or `"` in a name is a command separator on RRF — a name
#: like 'a.gcode"; M109 S300 ; "' would set the nozzle to 300C. Names are data,
#: never syntax: allow what real slicer output contains and nothing else.
_UNSAFE = re.compile(r'[;"\'`\\|&$<>\r\n\t\x00-\x1f]')


def safe_name(name: str) -> str:
    """Validate a printer-side filename. Raises ValueError on anything that
    could break out of a G-code argument or escape the upload directory."""
    if not name or not name.strip():
        raise ValueError("Filename is empty.")
    if len(name) > 255:
        raise ValueError("Filename is too long (max 255).")
    if _UNSAFE.search(name):
        raise ValueError(
            f"Refusing filename {name!r}: it contains characters that a "
            "printer would read as G-code syntax, not as a name.")
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(
            f"Refusing filename {name!r}: absolute paths and '..' could write "
            "outside the printer's upload directory.")
    return name


class Unsupported(RuntimeError):
    """The connected printer can't do this (firmware/protocol limit)."""


class NotConfigured(RuntimeError):
    """No printer is configured — the client should run printer_setup."""


@dataclass
class Target:
    """Everything needed to talk to one printer."""
    type: str
    host: str
    port: int | None = None
    api_key: str | None = None
    user: str | None = None
    password: str | None = None
    serial: str | None = None
    access_code: str | None = None
    source: str = "explicit"  # where these settings came from, for transparency

    @classmethod
    def from_dict(cls, d: dict, source: str) -> "Target":
        """Build from saved JSON, tolerating a hand-edited or stale file."""
        if not isinstance(d, dict) or not d.get("type") or not d.get("host"):
            raise NotConfigured(
                f"The saved printer config ({config_path()}) is missing "
                "'type' or 'host'. Delete it or run configure_printer again.")
        known = {f for f in cls.__dataclass_fields__ if f != "source"}
        unknown = set(d) - known
        if unknown:
            raise NotConfigured(
                f"The saved printer config ({config_path()}) has unknown "
                f"keys {sorted(unknown)}. Delete it or run configure_printer.")
        return cls(**d, source=source)

    def redacted(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        for secret in ("api_key", "password", "access_code"):
            if d.get(secret):
                d[secret] = "***"
        return d


def _pct(v) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _status(state, *, native=None, nozzle=None, nozzle_target=None, bed=None,
            bed_target=None, chamber=None, filename=None, layer=None,
            total_layers=None, progress=None, error=None, extra=None) -> dict:
    """The one status shape every backend returns."""
    out = {
        "state": state,
        "native_state": native,
        "print": {
            "filename": filename or None,
            "current_layer": layer,
            "total_layers": total_layers,
            "progress_pct": _pct(progress),
            "error_code": error,
        },
        "temps_c": {
            "nozzle": round(nozzle, 1) if nozzle is not None else None,
            "nozzle_target": nozzle_target,
            "bed": round(bed, 1) if bed is not None else None,
            "bed_target": bed_target,
        },
    }
    if chamber is not None:
        out["temps_c"]["chamber"] = round(chamber, 1)
    if extra:
        out.update(extra)
    return out


class Backend:
    """Base class. Subclasses implement the async methods they support."""

    name = "?"

    def __init__(self, target: Target):
        self.t = target

    def capabilities(self) -> set[str]:
        """Subset of {camera, files, layers, attributes}."""
        return set()

    async def close(self) -> None:
        pass

    async def status(self) -> dict:
        raise Unsupported(f"{self.name}: status not implemented")

    async def upload(self, path: Path, remote_name: str) -> str:
        raise Unsupported(f"{self.name}: upload not supported")

    async def start(self, filename: str) -> None:
        raise Unsupported(f"{self.name}: start not supported")

    async def pause(self) -> None:
        raise Unsupported(f"{self.name}: pause not supported")

    async def resume(self) -> None:
        raise Unsupported(f"{self.name}: resume not supported")

    async def stop(self) -> None:
        raise Unsupported(f"{self.name}: stop not supported")

    async def snapshot(self) -> bytes:
        raise Unsupported(f"{self.name}: this printer has no camera the "
                          "server can reach")

    async def files(self) -> list[str]:
        raise Unsupported(f"{self.name}: file listing not supported")

    async def attributes(self) -> dict:
        return {"backend": self.name, "host": self.t.host}


# ---------------------------------------------------------------- HTTP base


class _HttpBackend(Backend):
    port_default = 80
    timeout = 15.0

    def __init__(self, target: Target):
        super().__init__(target)
        self._client: httpx.AsyncClient | None = None

    @property
    def base(self) -> str:
        return f"http://{self.t.host}:{self.t.port or self.port_default}"

    def _headers(self) -> dict:
        return {}

    def _auth(self):
        return None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base, headers=self._headers(), auth=self._auth(),
                timeout=self.timeout, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _json(self, method: str, url: str, **kw):
        r = await self.http.request(method, url, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}


# ---------------------------------------------------------------- Moonraker


class MoonrakerBackend(_HttpBackend):
    """Klipper via Moonraker. Docs: moonraker.readthedocs.io external_api."""

    name = "moonraker"
    port_default = 7125
    _STATE = {"standby": IDLE, "printing": PRINTING, "paused": PAUSED,
              "complete": COMPLETE, "cancelled": STOPPED, "error": ERROR}

    def capabilities(self):
        return {"camera", "files", "layers", "attributes"}

    def _headers(self):
        return {"X-Api-Key": self.t.api_key} if self.t.api_key else {}

    async def status(self) -> dict:
        q = ("print_stats&extruder&heater_bed&virtual_sdcard&display_status")
        r = await self._json("GET", f"/printer/objects/query?{q}")
        d = (r.get("result") or {}).get("status")
        if not d:
            # Klippy disconnected / config error: say so instead of KeyError
            raise RuntimeError(
                "Moonraker returned no printer status — Klippy is probably "
                "disconnected or in an error state. Check Klipper's logs.")
        ps = d.get("print_stats") or {}
        info = ps.get("info") or {}
        ex, bed = d.get("extruder") or {}, d.get("heater_bed") or {}
        native = ps.get("state")
        state = self._STATE.get(native, BUSY)
        # Klipper says "printing" while it's still heating the first layer.
        # Fields can be null mid-restart, so compare only when both are real.
        temp, target = ex.get("temperature"), ex.get("target")
        if state == PRINTING and target and temp is not None and temp < target - 5:
            state = HEATING
        prog = d.get("virtual_sdcard", {}).get("progress")
        return _status(
            state, native=native,
            nozzle=ex.get("temperature"), nozzle_target=ex.get("target"),
            bed=bed.get("temperature"), bed_target=bed.get("target"),
            filename=ps.get("filename"),
            layer=info.get("current_layer"), total_layers=info.get("total_layer"),
            progress=prog * 100 if prog is not None else None,
            error=ps.get("message") or None)

    async def upload(self, path: Path, remote_name: str) -> str:
        # `print` defaults to false — deliberately not sent, so upload never
        # starts a job (verified: moonraker docs, file_manager upload table).
        with path.open("rb") as fh:
            r = await self.http.post(
                "/server/files/upload",
                files={"file": (remote_name, fh, "application/octet-stream")},
                data={"root": "gcodes"}, timeout=600.0)
        r.raise_for_status()
        return remote_name

    async def start(self, filename: str) -> None:
        await self._json("POST", "/printer/print/start",
                         params={"filename": filename})

    async def pause(self):
        await self._json("POST", "/printer/print/pause")

    async def resume(self):
        await self._json("POST", "/printer/print/resume")

    async def stop(self):
        await self._json("POST", "/printer/print/cancel")

    async def files(self) -> list[str]:
        d = await self._json("GET", "/server/files/list", params={"root": "gcodes"})
        return [f["path"] for f in d.get("result", [])]

    async def snapshot(self) -> bytes:
        d = await self._json("GET", "/server/webcams/list")
        cams = d.get("result", {}).get("webcams", [])
        if not cams:
            raise Unsupported("No webcam configured in Moonraker.")
        url = cams[0].get("snapshot_url") or ""
        if not url.startswith("http"):
            url = f"{self.base}/{url.lstrip('/')}"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, headers=self._headers())
            r.raise_for_status()
            return r.content

    async def attributes(self) -> dict:
        info = (await self._json("GET", "/printer/info")).get("result", {})
        srv = (await self._json("GET", "/server/info")).get("result", {})
        return {"backend": self.name, "host": self.t.host,
                "model": info.get("hostname"),
                "firmware": info.get("software_version"),
                "moonraker": srv.get("moonraker_version"),
                "klippy_state": info.get("state")}

    @staticmethod
    async def probe(host: str, client: httpx.AsyncClient) -> dict | None:
        try:
            r = await client.get(f"http://{host}:7125/server/info")
        except Exception:
            return None
        if r.status_code in (401, 403):
            return {"type": "moonraker", "port": 7125, "needs": ["api_key"]}
        if r.status_code == 200 and "moonraker_version" in r.text:
            return {"type": "moonraker", "port": 7125, "needs": []}
        return None


# ---------------------------------------------------------------- OctoPrint


class OctoPrintBackend(_HttpBackend):
    """OctoPrint REST API. Docs: docs.octoprint.org/en/master/api."""

    name = "octoprint"
    port_default = 5000

    def capabilities(self):
        return {"camera", "files", "attributes"}  # no layer counts

    def _headers(self):
        return {"X-Api-Key": self.t.api_key} if self.t.api_key else {}

    async def status(self) -> dict:
        try:
            p = await self._json("GET", "/api/printer", params={"history": "false"})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:  # OctoPrint: printer not connected
                return _status(ERROR, native="disconnected",
                               error="OctoPrint is not connected to the "
                                     "printer (409). Connect it in OctoPrint.")
            raise
        try:
            j = await self._json("GET", "/api/job")
        except httpx.HTTPError:
            j = {}
        flags = p.get("state", {}).get("flags", {})
        native = p.get("state", {}).get("text")
        if flags.get("error") or flags.get("closedOrError"):
            state = ERROR
        elif flags.get("paused") or flags.get("pausing"):
            state = PAUSED
        elif flags.get("cancelling"):
            state = STOPPED
        elif flags.get("printing"):
            state = PRINTING
        elif flags.get("operational") or flags.get("ready"):
            state = IDLE
        else:
            state = BUSY
        completion = (j.get("progress") or {}).get("completion")
        if state == IDLE and completion is not None and completion >= 100:
            # A finished job leaves the part on the plate; the start_print
            # plate-clear gate keys off COMPLETE. (A *cancelled* job can't be
            # told apart from idle via this API — see README known limits.)
            state = COMPLETE
        t = p.get("temperature") or {}
        tool, bed = t.get("tool0") or {}, t.get("bed") or {}
        return _status(
            state, native=native,
            nozzle=tool.get("actual"), nozzle_target=tool.get("target"),
            bed=bed.get("actual"), bed_target=bed.get("target"),
            filename=((j.get("job") or {}).get("file") or {}).get("name"),
            progress=completion,
            extra={"time": {"elapsed_s": (j.get("progress") or {}).get("printTime"),
                            "remaining_s": (j.get("progress") or {}).get("printTimeLeft")}})

    async def upload(self, path: Path, remote_name: str) -> str:
        # Sending neither `select` nor `print` means OctoPrint stores the file
        # and does nothing else (both default to false).
        with path.open("rb") as fh:
            r = await self.http.post(
                "/api/files/local",
                files={"file": (remote_name, fh, "application/octet-stream")},
                timeout=600.0)
        r.raise_for_status()
        return remote_name

    async def start(self, filename: str) -> None:
        await self._json("POST", f"/api/files/local/{filename}",
                         json={"command": "select"})
        await self._json("POST", "/api/job", json={"command": "start"})

    async def pause(self):
        await self._json("POST", "/api/job",
                         json={"command": "pause", "action": "pause"})

    async def resume(self):
        await self._json("POST", "/api/job",
                         json={"command": "pause", "action": "resume"})

    async def stop(self):
        await self._json("POST", "/api/job", json={"command": "cancel"})

    async def files(self) -> list[str]:
        d = await self._json("GET", "/api/files/local")
        return [f.get("path") for f in d.get("files", [])]

    async def snapshot(self) -> bytes:
        url = os.environ.get("PRINTER_SNAPSHOT_URL") or \
            f"http://{self.t.host}:8080/?action=snapshot"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.content

    async def attributes(self) -> dict:
        v = await self._json("GET", "/api/version")
        return {"backend": self.name, "host": self.t.host,
                "model": v.get("text"), "firmware": v.get("server"),
                "api": v.get("api")}

    @staticmethod
    async def probe(host: str, client: httpx.AsyncClient) -> dict | None:
        for port in (5000, 80):
            try:
                r = await client.get(f"http://{host}:{port}/api/version")
            except Exception:
                continue
            # /api/version needs a key; a 401/403 is itself the signature.
            if r.status_code in (401, 403):
                return {"type": "octoprint", "port": port, "needs": ["api_key"]}
            if r.status_code == 200 and '"api"' in r.text:
                return {"type": "octoprint", "port": port, "needs": []}
        return None


# ---------------------------------------------------------------- PrusaLink


class PrusaLinkBackend(_HttpBackend):
    """Prusa MK4/XL/MINI/CORE One. Docs: Prusa3D/Prusa-Link-Web openapi.yaml.
    Auth is HTTP Digest (user defaults to 'maker'); the password is the
    printer's PrusaLink password from its Settings > Network menu."""

    name = "prusalink"
    port_default = 80
    _STATE = {"IDLE": IDLE, "READY": IDLE, "BUSY": BUSY, "PRINTING": PRINTING,
              "PAUSED": PAUSED, "FINISHED": COMPLETE, "STOPPED": STOPPED,
              "ERROR": ERROR, "ATTENTION": ERROR}

    def capabilities(self):
        return {"camera", "files", "attributes"}  # v1 status has no layers

    def _auth(self):
        return httpx.DigestAuth(self.t.user or "maker", self.t.password or "")

    async def status(self) -> dict:
        d = await self._json("GET", "/api/v1/status")
        p, j = d.get("printer", {}), d.get("job", {})
        native = p.get("state")
        fname = None
        if j:
            try:
                fname = (await self._json("GET", "/api/v1/job")).get(
                    "file", {}).get("display_name")
            except httpx.HTTPError:
                pass
        nozzle_t = p.get("temp_nozzle")
        return _status(
            self._STATE.get(native, BUSY), native=native,
            nozzle=nozzle_t, nozzle_target=p.get("target_nozzle"),
            bed=p.get("temp_bed"), bed_target=p.get("target_bed"),
            filename=fname,
            progress=j.get("progress"),  # already percent (0-100)
            extra={"job_id": j.get("id"),
                   "time": {"elapsed_s": j.get("time_printing"),
                            "remaining_s": j.get("time_remaining")}})

    async def upload(self, path: Path, remote_name: str) -> str:
        # Print-After-Upload defaults to "?0" (no print); sent explicitly so a
        # firmware default change can't silently start a job.
        r = await self.http.put(
            f"/api/v1/files/usb/{remote_name}", content=path.read_bytes(),
            headers={"Content-Type": "application/octet-stream",
                     "Print-After-Upload": "?0", "Overwrite": "?1"},
            timeout=600.0)
        r.raise_for_status()
        return remote_name

    async def start(self, filename: str) -> None:
        # POST on the file path = "Start print of file if there's no print job
        # running" (openapi.yaml).
        await self._json("POST", f"/api/v1/files/usb/{filename}")

    async def _job_id(self) -> int:
        d = await self._json("GET", "/api/v1/status")
        jid = (d.get("job") or {}).get("id")
        if jid is None:
            raise Unsupported("No active job to control.")
        return jid

    async def pause(self):
        await self._json("PUT", f"/api/v1/job/{await self._job_id()}/pause")

    async def resume(self):
        await self._json("PUT", f"/api/v1/job/{await self._job_id()}/resume")

    async def stop(self):
        await self._json("DELETE", f"/api/v1/job/{await self._job_id()}")

    async def files(self) -> list[str]:
        d = await self._json("GET", "/api/v1/files/usb")
        return [f.get("name") for f in (d.get("children") or [])]

    async def snapshot(self) -> bytes:
        r = await self.http.get("/api/v1/cameras/snap")  # PNG, not JPEG
        r.raise_for_status()
        return r.content

    async def attributes(self) -> dict:
        d = await self._json("GET", "/api/v1/info")
        return {"backend": self.name, "host": self.t.host,
                "model": d.get("hostname") or d.get("name"),
                "firmware": d.get("firmware"), "serial": d.get("serial")}

    @staticmethod
    async def probe(host: str, client: httpx.AsyncClient) -> dict | None:
        for port in (80, 8080):
            try:
                r = await client.get(f"http://{host}:{port}/api/v1/status")
            except Exception:
                continue
            www = r.headers.get("www-authenticate", "").lower()
            if r.status_code == 401 and "digest" in www:
                return {"type": "prusalink", "port": port,
                        "needs": ["password"]}
            if r.status_code == 200 and '"printer"' in r.text:
                return {"type": "prusalink", "port": port, "needs": []}
        return None


# ---------------------------------------------------------------- Duet / RRF


class DuetBackend(_HttpBackend):
    """Duet 2/3 running RepRapFirmware.
    Docs: Duet3D/RepRapFirmware wiki, HTTP-requests."""

    name = "duet"
    port_default = 80
    _STATE = {"idle": IDLE, "processing": PRINTING, "printing": PRINTING,
              "paused": PAUSED, "halted": ERROR, "off": ERROR,
              "cancelling": STOPPED, "busy": BUSY, "pausing": BUSY,
              "resuming": BUSY, "changingTool": BUSY, "simulating": BUSY,
              "starting": BUSY, "updating": BUSY, "disconnected": ERROR}

    def capabilities(self):
        return {"files", "layers", "attributes"}  # no standard camera

    def __init__(self, target: Target):
        super().__init__(target)
        self._session = False

    async def _connect(self) -> None:
        # RepRapFirmware allows only a handful of concurrent sessions and never
        # reclaims ours until rr_disconnect. Connect once per backend instance;
        # close() releases it. (Reconnecting per call exhausted the pool.)
        if self._session:
            return
        r = await self.http.get("/rr_connect",
                                params={"password": self.t.password or "reprap"})
        r.raise_for_status()
        d = r.json()
        if d.get("err"):  # 0 = success; 1 = bad password; 2 = no free sessions
            raise RuntimeError(
                f"Duet rejected the connection (err={d['err']}: "
                + ("wrong password" if d["err"] == 1 else
                   "no free sessions — close a Duet Web Control tab and retry")
                + ").")
        if d.get("sessionKey") is not None:
            self.http.headers["X-Session-Key"] = str(d["sessionKey"])
        self._session = True

    async def close(self) -> None:
        if self._session and self._client is not None:
            try:
                await self.http.get("/rr_disconnect")
            except Exception:
                pass  # best effort; the session times out on its own
            self._session = False
        await super().close()

    async def _model(self, key: str) -> dict:
        d = await self._json("GET", "/rr_model", params={"key": key, "flags": "d99"})
        return d.get("result", {})

    async def status(self) -> dict:
        await self._connect()
        state, heat, job = (await self._model("state"),
                            await self._model("heat"), await self._model("job"))
        heaters = heat.get("heaters") or []
        bed_i = (heat.get("bedHeaters") or [0])[0]
        tool_i = 1 if len(heaters) > 1 else 0
        # bedHeaters is [-1] when no bed exists; a negative index would silently
        # report the LAST heater (the hotend) as the bed temperature.
        bed_h = heaters[bed_i] if 0 <= bed_i < len(heaters) else {}
        tool_h = heaters[tool_i] if tool_i < len(heaters) else {}
        native = state.get("status")
        mapped = self._STATE.get(native, BUSY)
        fobj = job.get("file") or {}
        size, pos = fobj.get("size"), job.get("filePosition")
        # RRF reports plain "idle" after a finished print, which would slip past
        # the plate-clear gate. job.lastFileName is set only once a file has
        # finished and nothing is printing — that means a part is on the plate.
        if mapped == IDLE and job.get("lastFileName"):
            mapped = STOPPED if job.get("lastFileAborted") else COMPLETE
        return _status(
            mapped, native=native,
            nozzle=tool_h.get("current"), nozzle_target=tool_h.get("active"),
            bed=bed_h.get("current"), bed_target=bed_h.get("active"),
            filename=fobj.get("fileName"),
            layer=job.get("layer"), total_layers=fobj.get("numLayers"),
            progress=(pos / size * 100) if size and pos is not None else None)

    async def upload(self, path: Path, remote_name: str) -> str:
        safe_name(remote_name)
        await self._connect()
        # Body is the raw file (not multipart) per the RRF wiki.
        r = await self.http.post("/rr_upload",
                                 params={"name": f"/gcodes/{remote_name}"},
                                 content=path.read_bytes(), timeout=600.0)
        r.raise_for_status()
        if r.json().get("err"):
            raise RuntimeError(f"Duet upload failed (err={r.json()['err']}) — "
                               "usually a full or unwritable SD card.")
        return remote_name

    async def _gcode(self, gcode: str) -> None:
        await self._connect()
        await self._json("GET", "/rr_gcode", params={"gcode": gcode})

    async def start(self, filename: str) -> None:
        # The filename lands inside a G-code argument: RRF treats an unescaped
        # `;` or `"` as a command separator, so a crafted name could append
        # arbitrary G-code (e.g. M109 S300). safe_name() rejects that syntax.
        safe_name(filename)
        await self._gcode(f'M32 "/gcodes/{filename}"')

    async def pause(self):
        await self._gcode("M25")

    async def resume(self):
        await self._gcode("M24")

    async def stop(self):
        await self._gcode("M25")   # cancelling mid-move needs a pause first
        await self._gcode("M0 H1")

    async def files(self) -> list[str]:
        await self._connect()
        d = await self._json("GET", "/rr_filelist", params={"dir": "/gcodes"})
        return [f.get("name") for f in d.get("files", []) if f.get("type") == "f"]

    async def attributes(self) -> dict:
        await self._connect()
        b = await self._model("boards")
        n = await self._model("network")
        board = (b or [{}])[0] if isinstance(b, list) else {}
        return {"backend": self.name, "host": self.t.host,
                "model": board.get("name") or n.get("name"),
                "firmware": board.get("firmwareVersion")}

    @staticmethod
    async def probe(host: str, client: httpx.AsyncClient) -> dict | None:
        try:
            r = await client.get(f"http://{host}/rr_connect",
                                 params={"password": "reprap"})
            if r.status_code == 200 and "err" in r.json():
                return {"type": "duet", "port": 80, "needs": []}
        except Exception:
            pass
        return None


# ---------------------------------------------------------------- Elegoo


class ElegooBackend(Backend):
    """Elegoo Centauri Carbon over SDCP, via pycentauri."""

    name = "elegoo"

    def __init__(self, target: Target):
        super().__init__(target)
        self._p = None
        self._control = False

    def capabilities(self):
        # CC1 firmware refuses file listing; pycentauri raises and we surface
        # that as Unsupported at call time.
        return {"camera", "layers", "files", "attributes"}

    async def _conn(self, control: bool = False):
        from pycentauri import connect_auto, discover
        if self._p is not None and self._control >= control:
            return self._p
        await self.close()
        mainboard_id = None
        try:
            for d in await discover(timeout=3.0):
                if getattr(d, "host", None) == self.t.host:
                    mainboard_id = getattr(d, "mainboard_id", None)
                    break
        except Exception:
            pass  # best effort; the firmware won't answer discovery mid-print
        self._p = await connect_auto(self.t.host, enable_control=control,
                                     mainboard_id=mainboard_id)
        self._control = control
        return self._p

    async def close(self):
        if self._p is not None:
            try:
                await self._p.close()
            finally:
                self._p = None

    @staticmethod
    def _map(code: int) -> str:
        from pycentauri.models import PrintStatus as S
        return {S.IDLE: IDLE, S.PRINTING: PRINTING, S.PAUSED: PAUSED,
                S.COMPLETED: COMPLETE, S.STOPPED: STOPPED, S.ERROR: ERROR,
                S.PREHEATING: HEATING, S.PREHEATING_COMPLETED: HEATING,
                # the CC1's long pre-print routine: all "busy", all active
                S.HOMING: BUSY, S.AUTO_LEVELING: BUSY, S.PRINT_START: BUSY,
                S.RESONANCE_TESTING: BUSY, S.FILE_CHECKING: BUSY,
                S.PRINTER_CHECKING: BUSY, S.AUTO_LEVELING_COMPLETED: BUSY,
                S.HOMING_COMPLETED: BUSY, S.RESONANCE_TESTING_COMPLETED: BUSY,
                S.PAUSING: BUSY, S.STOPPING: BUSY, S.RESUMING: BUSY,
                }.get(code, BUSY)

    @staticmethod
    def _native(code: int) -> str:
        from pycentauri.models import PrintStatus
        for n in dir(PrintStatus):
            if not n.startswith("_") and getattr(PrintStatus, n) == code:
                return n.lower()
        return f"unknown({code})"

    async def status(self) -> dict:
        s = await (await self._conn()).status()
        pi = s.print_info
        return _status(
            self._map(pi.status), native=self._native(pi.status),
            nozzle=s.temp_nozzle, nozzle_target=s.temp_nozzle_target,
            bed=s.temp_bed, bed_target=s.temp_bed_target,
            chamber=s.temp_chamber,
            filename=pi.filename, layer=pi.current_layer,
            total_layers=pi.total_layer, progress=pi.progress,
            error=pi.err_num,
            extra={"fans_pct": dict(s.fan_speed) if s.fan_speed else {},
                   "position": list(s.coord) if s.coord else None,
                   "time": {"elapsed_s": pi.current_ticks,
                            "remaining_s": (pi.total_ticks - pi.current_ticks)
                            if pi.total_ticks else None}})

    async def upload(self, path: Path, remote_name: str) -> str:
        safe_name(remote_name)
        p = await self._conn(control=True)
        try:
            return await p.upload_file(str(path), remote_name=remote_name)
        except Exception as e:
            if "500" in str(e):
                raise RuntimeError(
                    f"Upload failed: {e}. On the Centauri Carbon this means "
                    "the printer is busy or its storage is full — the firmware "
                    "can't say which, and can't delete files remotely. Wait for "
                    "idle and/or clear space on the touchscreen, then retry."
                ) from e
            raise

    async def start(self, filename: str) -> None:
        await (await self._conn(control=True)).start_print(filename)

    async def pause(self):
        await (await self._conn(control=True)).pause()

    async def resume(self):
        await (await self._conn(control=True)).resume()

    async def stop(self):
        await (await self._conn(control=True)).stop()

    async def snapshot(self) -> bytes:
        return await (await self._conn()).snapshot()

    async def files(self) -> list[str]:
        try:
            d = await (await self._conn()).list_files()
        except Exception as e:
            raise Unsupported(f"{e}") from e
        return [f.get("name", str(f)) for f in (d.get("files") or [])]

    async def attributes(self) -> dict:
        a = await (await self._conn()).attributes()
        d = {k: v for k, v in a.model_dump().items() if k != "raw"}
        return {"backend": self.name, "host": self.t.host, **d}

    @staticmethod
    async def probe(host: str, client: httpx.AsyncClient) -> dict | None:
        try:
            fut = asyncio.open_connection(host, 3030)
            r, w = await asyncio.wait_for(fut, timeout=2.0)
            w.close()
            await w.wait_closed()
            return {"type": "elegoo", "port": 3030, "needs": []}
        except Exception:
            return None


# ---------------------------------------------------------------- Bambu Lab


class BambuBackend(Backend):
    """Bambu Lab in LAN mode via the optional bambulabs-api extra.

    EXPERIMENTAL: written against bambulabs-api's documented surface but not
    verified on real hardware by this project. Needs the printer's serial and
    LAN access code (printer screen: Settings > Network > LAN Only Mode).
    """

    name = "bambu"

    def __init__(self, target: Target):
        super().__init__(target)
        self._p = None
    _STATE = {"IDLE": IDLE, "PREPARE": HEATING, "RUNNING": PRINTING,
              "PAUSE": PAUSED, "FINISH": COMPLETE, "FAILED": ERROR}

    def capabilities(self):
        return {"camera", "layers", "attributes"}

    async def _conn(self):
        if self._p is not None:
            return self._p
        try:
            import bambulabs_api
        except ImportError as e:
            raise Unsupported(
                "Bambu support needs the optional extra: "
                "pip install 'orcaslicer-mcp[bambu]'") from e
        if not (self.t.serial and self.t.access_code):
            raise NotConfigured(
                "Bambu printers need both serial and access_code (printer "
                "screen: Settings > Network > LAN Only Mode).")
        p = bambulabs_api.Printer(self.t.host, self.t.access_code, self.t.serial)
        await asyncio.to_thread(p.connect)
        for _ in range(20):  # MQTT needs a moment before the first report
            if await asyncio.to_thread(p.mqtt_client_ready):
                break
            await asyncio.sleep(0.5)
        self._p = p
        return p

    async def close(self):
        if self._p is not None:
            try:
                await asyncio.to_thread(self._p.disconnect)
            finally:
                self._p = None

    async def status(self) -> dict:
        p = await self._conn()
        g = lambda fn, *a: asyncio.to_thread(fn, *a)
        raw = await g(p.get_current_state)
        # older bambulabs-api stringifies as "GcodeState.RUNNING", newer as
        # "RUNNING"; .name is stable across both.
        native = getattr(raw, "name", None) or str(raw).rsplit(".", 1)[-1]
        return _status(
            self._STATE.get(native.upper(), BUSY), native=native,
            nozzle=await g(p.get_nozzle_temperature),
            bed=await g(p.get_bed_temperature),
            chamber=await g(p.get_chamber_temperature),
            filename=await g(p.get_file_name),
            layer=await g(p.current_layer_num),
            total_layers=await g(p.total_layer_num),
            progress=await g(p.get_percentage),
            error=await g(p.print_error_code))

    async def upload(self, path: Path, remote_name: str) -> str:
        safe_name(remote_name)
        p = await self._conn()
        with path.open("rb") as fh:
            await asyncio.to_thread(p.upload_file, fh, remote_name)
        return remote_name

    async def start(self, filename: str, plate: int = 1) -> None:
        safe_name(filename)
        p = await self._conn()
        ok = await asyncio.to_thread(p.start_print, filename, plate)
        if ok is False:
            raise RuntimeError("Printer rejected the start command.")

    async def pause(self):
        await asyncio.to_thread((await self._conn()).pause_print)

    async def resume(self):
        await asyncio.to_thread((await self._conn()).resume_print)

    async def stop(self):
        await asyncio.to_thread((await self._conn()).stop_print)

    async def snapshot(self) -> bytes:
        p = await self._conn()
        return await asyncio.to_thread(p.get_camera_image)

    async def attributes(self) -> dict:
        p = await self._conn()
        return {"backend": self.name, "host": self.t.host,
                "serial": self.t.serial,
                "nozzle_diameter": await asyncio.to_thread(p.nozzle_diameter),
                "print_type": await asyncio.to_thread(p.print_type)}

    @staticmethod
    async def probe(host: str, client: httpx.AsyncClient) -> dict | None:
        try:  # MQTT over TLS is the LAN-mode signature
            fut = asyncio.open_connection(host, 8883)
            r, w = await asyncio.wait_for(fut, timeout=2.0)
            w.close()
            await w.wait_closed()
            return {"type": "bambu", "port": 8883,
                    "needs": ["serial", "access_code"]}
        except Exception:
            return None


BACKENDS = {"moonraker": MoonrakerBackend, "octoprint": OctoPrintBackend,
            "prusalink": PrusaLinkBackend, "duet": DuetBackend,
            "elegoo": ElegooBackend, "bambu": BambuBackend}


def make(target: Target) -> Backend:
    if target.type not in BACKENDS:
        raise ValueError(f"Unknown printer type {target.type!r}. "
                         f"Supported: {', '.join(TYPES)}")
    return BACKENDS[target.type](target)


# ---------------------------------------------------------------- discovery


async def elegoo_broadcast(timeout: float = 3.0) -> list[dict]:
    """Elegoo printers answer a UDP broadcast, so they self-announce."""
    try:
        from pycentauri import discover
        return [{"host": d.host, "type": "elegoo",
                 "model": getattr(d, "machine_name", None),
                 "firmware": getattr(d, "firmware_version", None)}
                for d in await discover(timeout=timeout)]
    except Exception:
        return []


async def probe_host(host: str) -> dict | None:
    """Identify which protocol a host speaks. Read-only probes only."""
    async with httpx.AsyncClient(timeout=2.5, follow_redirects=True) as c:
        for cls in (ElegooBackend, MoonrakerBackend, PrusaLinkBackend,
                    OctoPrintBackend, DuetBackend, BambuBackend):
            try:
                hit = await cls.probe(host, c)
            except Exception:
                hit = None
            if hit:
                return {"host": host, **hit}
    return None


# ---------------------------------------------------------------- config


def config_path() -> Path:
    if os.environ.get("ORCASLICER_MCP_CONFIG"):
        return Path(os.environ["ORCASLICER_MCP_CONFIG"]).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "orcaslicer-mcp" / "printer.json"
    return Path.home() / ".config" / "orcaslicer-mcp" / "printer.json"


def load_config() -> dict | None:
    p = config_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_config(target: Target) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {k: v for k, v in target.__dict__.items()
         if v is not None and k != "source"}
    p.write_text(json.dumps(d, indent=2))
    if os.name != "nt":
        p.chmod(0o600)  # it holds API keys / access codes
    # On Windows chmod can't express this; the file inherits the parent ACL.
    return p


def from_env() -> Target | None:
    if not (os.environ.get("PRINTER_TYPE") and os.environ.get("PRINTER_HOST")):
        return None
    port = os.environ.get("PRINTER_PORT")
    return Target(type=os.environ["PRINTER_TYPE"].lower(),
                  host=os.environ["PRINTER_HOST"],
                  port=int(port) if port else None,
                  api_key=os.environ.get("PRINTER_API_KEY"),
                  user=os.environ.get("PRINTER_USER"),
                  password=os.environ.get("PRINTER_PASSWORD"),
                  serial=os.environ.get("PRINTER_SERIAL"),
                  access_code=os.environ.get("PRINTER_ACCESS_CODE"),
                  source="env")


def from_orca_preset(preset: dict, name: str) -> Target | None:
    """Build a target from an OrcaSlicer machine preset — Orca already stores
    the printer's host and protocol when network printing is set up."""
    host = preset.get("print_host")
    if not host:
        return None
    host = host.replace("http://", "").replace("https://", "").strip("/")
    raw = (preset.get("host_type") or "").lower()
    ptype = ORCA_HOST_TYPE.get(raw)
    if not ptype:
        if raw in ORCA_HOST_TYPE_UNSUPPORTED:
            raise Unsupported(
                f"Your OrcaSlicer preset {name!r} uses host type {raw!r}. "
                f"{ORCA_HOST_TYPE_UNSUPPORTED[raw]}")
        return None
    port = preset.get("printhost_port")
    return Target(type=ptype, host=host,
                  port=int(port) if str(port or "").isdigit() else None,
                  api_key=preset.get("printhost_apikey") or None,
                  user=preset.get("printhost_user") or None,
                  password=preset.get("printhost_password") or None,
                  source=f"OrcaSlicer preset {name!r}")
