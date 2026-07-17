# Contributing

Thanks for helping. This is a small, deliberately lazy codebase — two modules,
one test file, no framework beyond FastMCP. Keep changes minimal and tested.

## Running the tests

```bash
uv venv --python 3.11 && uv pip install -e .
.venv/bin/python test_server.py
```

The two slicing tests self-skip without OrcaSlicer installed. Everything else —
backends, config resolution, `watch_print`, and the end-to-end fixture tests —
runs with no printer and no OrcaSlicer, so CI covers it (see the `tests` badge).

## Adding a printer backend

Almost everything is in [`backends.py`](backends.py). To add a protocol:

1. Subclass `Backend` (or `_HttpBackend` for a REST protocol — it gives you a
   pooled `httpx.AsyncClient` via `self.http` and a `base` URL from the target).
2. Implement the async methods you can: `status`, `upload`, `start`, `pause`,
   `resume`, `stop`, and optionally `snapshot`, `files`, `attributes`.
   `status()` **must** return the normalized shape via the `_status(...)` helper
   so the tools and `watch_print` work unchanged.
3. Map the firmware's states onto the shared vocabulary (`IDLE`, `HEATING`,
   `PRINTING`, `PAUSED`, `COMPLETE`, `STOPPED`, `ERROR`, `BUSY`). A finished job
   must map to `COMPLETE`/`STOPPED`, not `IDLE` — the plate-clear safety gate
   depends on it.
4. Add a static `probe(host, client)` that identifies your printer with a fast,
   **unauthenticated, read-only** request.
5. Register it in `BACKENDS` and `TYPES`, and in `ORCA_HOST_TYPE` if OrcaSlicer
   has a matching `host_type`.

Then prove it end-to-end: add a fixture handler in
[`test_fixtures.py`](test_fixtures.py) and an `_e2e` test in `test_server.py`.

## Safety rules a backend must uphold

This code heats hardware and starts multi-day jobs. Non-negotiable:

- **`upload()` must never start a print.** Suppress your protocol's
  start-on-upload flag explicitly, and add a test asserting the flag isn't sent.
- **Run every printer-side filename through `safe_name()`** before it reaches
  firmware or a URL. A filename is data, never G-code syntax.
- **`status()` must fail loud**, not fabricate — if the printer is unreachable
  or disconnected, say so; don't return a fake "idle".

## Style

Match the surrounding code: small diffs, comments only where the *why* isn't
obvious, and a runnable check for any non-trivial logic. If you're simplifying
deliberately, say so in a one-line comment.
