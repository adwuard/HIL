# Logic-Analyzer Capture Daemon — Protocol & Integration Spec

This is the handoff contract between **HIL** (MIT, Python) and a **logic-analyzer
capture daemon** (GPLv3, C++). HIL never links GPL code and never touches the
timing-critical USB path: it is a thin **JSON-RPC client**; the daemon owns the
device, does the real-time capture and the protocol decoding, and hands back
**file references** (artifacts), not bulk bytes.

The first target is the **ALIENTEK ATK-Logic** analyzer, whose only reference
implementation is the `atk-logic` Qt app (proprietary libusb protocol +
`atk_libsigrokdecode`). The daemon is a headless `--server` build of that app.

```
+-------------------+   logic://host:5555    +----------------------------+
|  HIL (MIT, Py)    |  JSON-RPC 2.0 / TCP    |  capture daemon (GPLv3)     |
|  hil.drivers.logic| <--------------------> |  atk-logic --server         |
|  client + bench   |   control + metadata   |  libusb capture + decode    |
+-------------------+                        +-------------+--------------+
        |                                                  |
        |  references artifact by path                     |  writes captures
        v                                                  v
   CaptureArtifact  <-------------------------------->  spool dir (disk)
```

## Why a separate process
- **License isolation** — the capture engine and `atk_libsigrokdecode` are GPLv3;
  HIL's core is MIT. A socket boundary keeps them cleanly separate (same pattern
  HIL already uses for the optional GPL `ppk2-api` backend).
- **Timing** — logic capture is a high-throughput, latency-sensitive libusb
  stream (the ATK app queues 40 x 16 KB async transfers). That must run in a
  dedicated process/thread, not inside HIL's control loop.
- **Data volume** — seconds at tens of MS/s is MBs-GBs. Data is streamed to disk
  and referenced by path; only metadata crosses the socket.
- **Long-lived session** — one persistent connection owns the device across many
  captures, avoiding slow/fragile repeated USB open-close and contention with the
  GUI.

## Transport & framing
- **TCP**, default port **5555** (HIL address: `logic://host[:port]`).
- **Newline-delimited JSON-RPC 2.0**: one JSON object per line, `\n`-terminated,
  UTF-8. Requests carry `jsonrpc`, `id`, `method`, optional `params`. Responses
  carry the matching `id` and either `result` or `error`.
- The daemon **may** emit unsolicited notifications (no `id`, e.g. progress); the
  client ignores any line whose `id` does not match the pending request.
- Non-JSON banner lines are ignored by the client.

## Session, ownership & liveness
- **Single owner**: the device is held by one client at a time. `hello` returns
  `busy: true` if the GUI (or another client) currently owns it; the client should
  surface this rather than fight for the device.
- **Persistent connection**: the client opens once and keeps the socket for the
  whole bench run; `close` releases the device and ends the session.
- **Heartbeat**: the client sends `ping` periodically; the daemon replies
  `{ "pong": true }`. On a dropped socket the client reconnects with backoff and
  re-issues `hello`.
- **Spool**: the daemon has a scratch directory (its `spool_dir`, reported by
  `hello`) where captures land. HIL mirrors this with its own `ArtifactSpool`
  (`$HIL_SPOOL_DIR` or `./.hil-spool`) and applies TTL cleanup / explicit retain.

## Methods

### `hello` -> capabilities
Result:
```json
{
  "protocol": "1",
  "device": {"vendor": "ALIENTEK", "model": "ATK-Logic", "serial": "...", "variant": "MCU|FPGA"},
  "channels": 16,
  "max_sample_rate_hz": 200000000,
  "depths": [4096, 65536, 1048576],
  "decoders": ["i2c", "spi", "uart", "1wire", "can", "..."],
  "spool_dir": "/path/to/spool",
  "busy": false
}
```
`decoders` is empty on builds where decoding is unavailable (see Packaging); HIL's
`decode` step then reports a clear "unavailable" error.

### `configure` -> `{ "ok": true, "config": {...} }`
Params (all optional; last config wins, merged daemon-side):
```json
{
  "channels": 8,                 // int count OR list of indices [0,1,2,3]
  "sample_rate_hz": 4000000,
  "depth_samples": 1048576,      // OR duration_s (one of the two)
  "duration_s": 0.5,
  "trigger": {"channel": 0, "level": 1, "edge": "rising|falling", "position_pct": 10},
  "mode": "block"                // "block" (fill buffer) | "stream" (continuous)
}
```
Maps to the ATK protocol's `ParameterSetting` + `SimpleTrigger`.

### `arm` -> `{ "capture_id": "..." }`
Configures hardware and arms the trigger / starts the fill (ATK `Register` +
bulk `Read` loop). Non-blocking.

### `status` params `{ "capture_id": "..." }` -> state
```json
{"state": "idle|armed|capturing|triggered|done|error", "progress": 0.0, "samples_captured": 0, "error": "optional"}
```

### `wait` params `{ "capture_id": "...", "timeout_s": 30 }` -> state
Blocks until `done`/`error`/timeout, then returns a `status`-shaped object.

### `fetch` -> `{ "artifact": {...} }`
Params:
```json
{"capture_id": "...", "format": "srzip|csv|raw", "dest": "spool|path", "path": "/optional/dest"}
```
Large-data handling:
- `dest: "spool"` (default) — the daemon writes into its `spool_dir` and returns
  the path. Fastest when client and daemon share a filesystem (same host).
- `dest: "path"` — the daemon writes to the client-supplied `path` (a shared or
  mounted location).
- Remote daemons without a shared filesystem should stream the file in chunks to
  `path` (out of band of the JSON-RPC control channel) or expose an HTTP range
  fetch; the control reply still returns only artifact **metadata**.

Result `artifact`:
```json
{"id": "...", "kind": "logic", "path": "/spool/cap-....srzip", "format": "srzip",
 "channels": [0,1], "sample_rate_hz": 4000000, "sample_count": 2000000,
 "duration_s": 0.5, "bytes": 123456, "trigger": {"channel": 0, "edge": "rising"}}
```

### `decode` -> `{ "artifact": {...} }`
Params:
```json
{"artifact": "/spool/cap-....srzip", "decoders": [
   {"name": "i2c", "options": {}, "channels": {"scl": 0, "sda": 1}}
 ], "format": "csv|json", "out": "/optional/dest"}
```
Runs `atk_libsigrokdecode` (the same decoders as `runtime/decoders/`). Result:
```json
{"id": "...", "kind": "decoded", "path": "/spool/dec-....csv", "format": "csv",
 "frames": 42, "decoders": ["i2c"], "parent": "<source artifact id>", "bytes": 4096}
```

### `stop` params `{ "capture_id": "..." }`
Aborts an in-progress capture (ATK `Stop`).

### `close`
Releases the device lock and ends the session.

### `ping` -> `{ "pong": true }`
Heartbeat.

## Errors
Standard JSON-RPC error object; the client raises `HilError` with the message:
```json
{"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "device busy"}}
```
Suggested codes: `-32001` device busy, `-32002` not configured, `-32003` capture
failed/timeout, `-32004` decoder unavailable, `-32005` bad parameter.

## Example transcript
```
-> {"jsonrpc":"2.0","id":1,"method":"hello"}
<- {"jsonrpc":"2.0","id":1,"result":{"protocol":"1","device":{"vendor":"ALIENTEK","model":"ATK-Logic"},"channels":16,"decoders":["i2c","uart"],"spool_dir":"/tmp/atk-spool","busy":false}}
-> {"jsonrpc":"2.0","id":2,"method":"configure","params":{"channels":[0,1],"sample_rate_hz":4000000,"duration_s":0.5,"trigger":{"channel":0,"edge":"rising","position_pct":10}}}
<- {"jsonrpc":"2.0","id":2,"result":{"ok":true}}
-> {"jsonrpc":"2.0","id":3,"method":"arm"}
<- {"jsonrpc":"2.0","id":3,"result":{"capture_id":"cap-1"}}
-> {"jsonrpc":"2.0","id":4,"method":"wait","params":{"capture_id":"cap-1","timeout_s":5}}
<- {"jsonrpc":"2.0","id":4,"result":{"state":"done","samples_captured":2000000}}
-> {"jsonrpc":"2.0","id":5,"method":"fetch","params":{"capture_id":"cap-1","format":"srzip","dest":"spool"}}
<- {"jsonrpc":"2.0","id":5,"result":{"artifact":{"id":"a1","kind":"logic","path":"/tmp/atk-spool/a1.srzip","format":"srzip","sample_count":2000000,"sample_rate_hz":4000000}}}
-> {"jsonrpc":"2.0","id":6,"method":"decode","params":{"artifact":"/tmp/atk-spool/a1.srzip","decoders":[{"name":"i2c","channels":{"scl":0,"sda":1}}],"format":"csv"}}
<- {"jsonrpc":"2.0","id":6,"result":{"artifact":{"id":"d1","kind":"decoded","path":"/tmp/atk-spool/d1.csv","frames":42,"parent":"a1"}}}
-> {"jsonrpc":"2.0","id":7,"method":"close"}
<- {"jsonrpc":"2.0","id":7,"result":{"ok":true}}
```

## Cross-platform build & packaging
The daemon is built from the `atk-logic` Qt project, which already carries
per-platform scopes and is inherently cross-platform:
- `QT += ... network` is present, so a `QTcpServer` JSON-RPC front-end is a small
  addition; no new third-party dependency.
- Existing build scopes: `win32` (Windows), `macx` (macOS), `unix:!android`
  (Linux), `android` (NDK cross-build + JNI OTG bridge `android_usb_bridge.cpp`).
- **Capture-only daemon** depends just on `libusb-1.0` (portable; Android needs
  `libusb_wrap_sys_device` >= 1.0.24). It cross-compiles cleanly to fixed
  per-platform binaries, including Android.
- **Decode** pulls in `atk_libsigrokdecode` + glib + embedded CPython
  (`python3-embed`) — the heavy, per-platform part. Upstream already disables it
  on Android "Phase 1" via `android/stub_include/atk_decoder.h` (no-op stubs,
  `ATK_NO_DECODER`), reporting "unavailable" at runtime.

Packaging guidance:
- Ship a **capture-only** fixed binary per platform as the baseline (P1); it is
  small and cross-compiles.
- Ship **decode-capable** builds where CPython+glib bundle cleanly (desktop
  first). On platforms without decode, return `hello.decoders = []`; HIL's
  `decode` step then fails with a clear "decoder unavailable" message rather than
  silently misbehaving.
- HIL itself is pure Python (already cross-platform) and needs no packaging
  changes — it only speaks the socket protocol above.

## Mapping to `atk-logic` internals (implementation notes)
- Device discovery/ownership: `pv/usb/usb_server.*` (the in-process multi-device
  manager) + `pv/usb/usb_hotplug.*`.
- Capture control: `pv/usb/usb_control.*` — `ParameterSetting`, `SimpleTrigger`,
  `Register`, bulk `Read`, `Stop`, `Exit`.
- Session/data model + CSV export: `pv/data/session.*`, `pv/data/segment.*`,
  `pv/controller/session_controller.*`.
- Decode: `pv/data/decode_controller.*`, `pv/static/decode_service.*` over
  `atk_libsigrokdecode` (`runtime/decoders/`).
- New: a `--server` entry (headless, `QCoreApplication`) exposing the JSON-RPC
  methods above; reuse the existing capture/decode paths unchanged.

## Phasing
- **P1** — headless `--server`: `hello`, `configure`, `arm`, `status`, `wait`,
  `fetch` (spool + path), `stop`, `close`, `ping`. Capture-only, fixed binaries.
- **P2** — hardware `trigger` (edge/level/position) + `stream` mode with
  chunked/remote fetch.
- **P3** — `decode` via `atk_libsigrokdecode`; per-platform decode packaging with
  graceful `decoders: []` fallback.

HIL already implements the client side of P1-P3 (against this contract) plus a
simulator backend, so the full bench flow is testable before the daemon exists.
