# ATK-Logic-Analyzer — In-Process Backend (`logic://usb`)

This is the guide for driving an **ALIENTEK ATK-Logic-Analyzer** (USB
`VID:PID 1A86:FFCC`, 16 channels, ≤200 MS/s) **directly inside HIL** — no capture
daemon, no C++ build, no GPL boundary.

HIL vendors the **MIT** [`atk-logic-mcp`](https://github.com/hugelzhang/atk-logic-mcp)
core under `hil._vendor.atklogic` and wraps it in an in-process
`LogicBackend`. Because that core is **pure Python**, it runs anywhere HIL runs
(macOS / Linux / Windows) and there is nothing to cross-compile. Captures are still
**file-backed artifacts** (a path + metadata, never bulk bytes in memory), so the
same `LogicInstrument` facade, pseudo-SCPI command set, bench `logic`/`decode`
steps, simulator and `--json` reporting all work unchanged.

> Prefer an out-of-process engine (device isolation, a C++/GPL capture daemon on
> `logic://host:5555`)? That path still exists — see
> [`logic-daemon-protocol.md`](logic-daemon-protocol.md). This document covers the
> **default, in-process** backend.

```
+------------------------------+        +--------------------------------------+
|  HIL (MIT, Python)           |        |  hil._vendor.atklogic  (MIT, vendored)|
|  connect("logic://usb")      |        |                                      |
|   LogicInstrument            |        |   atkproto  -> raw USB device layer   |
|    -> AtkLogicBackend  ------+------->|   atkdl     -> .atkdl capture file    |
|       -> AtkCore             |  in    |   srdhost   -> pure-Python sigrok PDs |
|  hil atklogic ... --json -----+ proc  |                                      |
+------------------------------+        +------------------+-------------------+
                                                           |  pyusb + libusb
                                                           v
                                            ATK-Logic-Analyzer  (1A86:FFCC)
```

## 1. Setup

The vendored core ships with HIL; only two **optional** runtime pieces are needed:

| Piece | Why | Install |
|---|---|---|
| `pyusb` | USB access from Python | `pip install 'hil-harness[atk]'` (or plain `pip install pyusb`) |
| `libusb-1.0` | the native library pyusb loads | macOS `brew install libusb` · Debian/Ubuntu `apt install libusb-1.0-0-dev` · Fedora `dnf install libusb1-devel` · Windows `choco install libusb` |
| sigrok decoders dir | **only** for protocol decode | ships with the ATK-Logic desktop app; or any `libsigrokdecode` PD tree |

**Cold start (fresh clone).** Install HIL plus the `atk` extra, then the native
`libusb` runtime — that is the whole setup:

```bash
pip install -e '.[atk]'          # or: uv pip install -e '.[atk]'   (pulls pyusb)
brew install libusb              # macOS · Linux: apt/dnf install libusb-1.0-0-dev
hil atklogic info                # confirm the analyzer is attached
```

Or let HIL manage the Python side for you (the privileged `libusb` step is gated
behind `--yes`; in uv-managed virtualenvs that ship no pip, provisioning installs
with `uv pip` automatically):

```bash
hil setup alientek-atk-logic --yes      # pyusb (+ libusb with --yes)
hil setup alientek-atk-logic --check    # am I ready?  -> "[ok] import usb"
hil doctor                              # backend + provisioning status (license: MIT)
```

`pyusb` and `libusb` are imported **lazily** (only when a device is first opened),
so `import hil` never requires them and the offline simulator/tests run without
them.

### Environment overrides

| Var | Effect |
|---|---|
| `ATK_LIBUSB_DLL` | explicit path to `libusb-1.0.{dylib,so,dll}` (else auto-discovered: Homebrew/system prefixes on macOS/Linux, the bundled DLL on Windows) |
| `ATK_DECODERS_DIR` | sigrok decoders directory (else the platform default install locations are probed) |
| `ATK_ALLOW_MULTI` | bypass the single-instance guard (the device is normally opened by one process at a time) |

Confirm everything — including that the analyzer is physically attached:

```bash
hil atklogic info
# ATK ATK-Logic-Analyzer  SN ATK22
#   usb_id:    1A86:FFCC
#   channels:  16
#   rates_mhz: [1, 2, 4, 5, 10, 20, 25, 40, 50, 100, 200]
#   max_rate:  200000000 Hz
#   libusb:    /opt/homebrew/opt/libusb/lib/libusb-1.0.dylib
#   decoders:  /Applications/ATK-Logic.app/Contents/MacOS/decoders
```

## 2. Addressing

```
logic://usb                                      # first ATK-Logic found
logic://usb?serial=ATK22                         # select one analyzer by serial
logic://usb?decoders=/path/to/decoders           # explicit sigrok PD dir
logic://usb?libusb=/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib
logic://usb?serial=ATK22&decoders=/p&libusb=/p   # all three
```

`logic://host[:port]` (e.g. `logic://127.0.0.1:5555`) still selects the JSON-RPC
**daemon** backend; the host tokens `usb` / `local` / `atk` select this
**in-process** backend.

## 3. Python API

```python
from hil import connect

la = connect("logic://usb", device_slug="alientek-atk-logic")
la.write(":CONF:CHAN 0,1")        # or :CONF:CHAN 8  (a channel count)
la.write(":CONF:RATE 4000000")    # Hz; snapped to the nearest device gear
la.write(":TRIG:CHAN 0"); la.write(":TRIG:EDGE R")

wave = la.run_capture(duration_s=0.5, fmt="atkdl")   # -> CaptureArtifact
dec  = la.decode({"name": "i2c", "channels": {"scl": 0, "sda": 1}})  # -> decoded frames
print(wave.path, wave.sample_count, wave.sample_rate_hz)
print(dec.path, dec.frames)
la.close()
```

The backend is a `LogicBackend` like any other, so `device_slug="alientek-atk-logic"`
brings the safety envelope (channels `1..16`, rate `1..200000000 Hz`) and bench
integration along for free.

## 4. CLI (`hil atklogic`)

Every command speaks `--json` (agent-friendly) and has a human default.

```bash
hil atklogic info                                   # identify the attached analyzer
hil atklogic list [filter]                          # sigrok decoders (offline; no device)
hil atklogic capture --channels 0,1 --rate 4e6 --depth 200000 \
                     --trigger rising --mode buffer --fmt atkdl --out cap.atkdl
hil atklogic decode cap.atkdl --decoder i2c --map scl=0,sda=1 --out frames.csv
hil atklogic decode cap.atkdl                       # use the .atkdl's embedded config
hil atklogic convert cap.atkdl --fmt csv --out cap.csv   # offline re-serialize
```

`capture` flags: `--channels` (`0`, `0,1`, `ch0,ch7`), `--rate` (Hz, snapped to a
device gear), `--depth` (samples), `--threshold` (volts, default 1.5), `--trigger`
(`rising|falling|any|high|low`), `--mode` (`buffer|trigger`), `--rle`, `--fmt`
(`atkdl|csv|raw`), `--out`. Plus `--serial` / `--decoders` / `--libusb` on any
subcommand.

A **standalone** entry point delegates to the same parser (handy for embedding or
an MCP server that should not depend on the `hil` console script):

```bash
python -m hil.drivers._atklogic info --json
python -m hil.drivers._atklogic capture --channels 0 --depth 100000 --out c.atkdl --json
```

Both the CLI and the driver call the **same** `AtkCore`
(`hil.drivers._atklogic`), so they can never drift.

## 5. Capture formats

| `--fmt` / `format=` | On disk | Notes |
|---|---|---|
| `atkdl` | `.atkdl` | **default**; the vendor container (a zip). Opens in ATK-Logic, carries the sample rate + embedded decoder config, and is the natural decode source. HIL's `srzip` request is served as `.atkdl`. |
| `csv` | `.csv` | HIL-compatible columns `index,time_s,ch<N>...`; interchangeable with the sim/daemon CSVs. |
| `raw` | `.bin` | packed primary-channel bytes (LSB-first, 8 samples/byte). |

## 6. Protocol decode

Decode runs **offline** over a saved artifact using the vendored pure-Python
sigrok host (`srdhost`) — no `libsigrokdecode` C library.

- **From an `.atkdl` with an embedded config** (set in the ATK-Logic GUI):
  `hil atklogic decode cap.atkdl` — the chain, channel map and options come from
  the file.
- **Manual** (any `.atkdl`/`.csv`): name a decoder stack and map its signals to
  hardware channels:

  ```bash
  hil atklogic decode cap.atkdl --decoder uart --map rx=0 --option baudrate=115200
  hil atklogic decode cap.atkdl --decoder i2c --map scl=0,sda=1 --out i2c.csv
  ```

  `--decoder` repeats to build a stack (e.g. `--decoder i2c --decoder eeprom24xx`);
  the **first** decoder takes `--map` / `--option`. `.csv` sources derive the sample
  rate from the `time_s` column; a raw `.bin` cannot be decoded (no rate) — capture
  as `.atkdl` or `.csv` instead.

`hil atklogic list` enumerates the available decoders (182 with the ATK-Logic
desktop bundle). Decode output is a decoded artifact: `csv`
(`sample,t_ms,protocol,row,text`) or `json` (full annotations + transactions).

## 7. Bench integration

```yaml
device: alientek-atk-logic
steps:
  - write: ":CONF:CHAN 0,1"
  - write: ":CONF:RATE 4000000"
  - logic:  {seconds: 0.5, format: atkdl, trigger: {channel: 0, edge: rising}, save_as: wave}
  - decode: {decoder: i2c, map: {scl: 0, sda: 1}, save_as: frames}
  - assert: {that: frames, op: gt, value: 0}
```

Run it against the simulator with **zero hardware** (no `--address`), or against the
device by pointing the run at `logic://usb`. See
`examples/bench/atk-logic-i2c-decode.yaml`.

## 8. Agent / MCP registration

HIL's CLI is the agent surface: **every command is one shot and speaks `--json`**,
so an MCP server (or any agent) wraps it by shelling out and parsing stdout. No
daemon, no session state — each invocation opens the device, does the work, and
closes it.

Example MCP tool handlers (pseudo-code):

```python
import json, subprocess

def atklogic_capture(channels="0", rate=1e6, depth=100000, fmt="atkdl", out="cap.atkdl"):
    cp = subprocess.run(
        ["hil", "atklogic", "capture", "--channels", str(channels), "--rate", str(rate),
         "--depth", str(depth), "--fmt", fmt, "--out", out, "--json"],
        capture_output=True, text=True)
    return json.loads(cp.stdout)          # {ok, path, sample_count, sample_rate_hz, stats, ...}

def atklogic_decode(artifact, decoder=None, map=None, out=None):
    argv = ["hil", "atklogic", "decode", artifact, "--json"]
    if decoder: argv += ["--decoder", decoder]
    if map:     argv += ["--map", map]
    if out:     argv += ["--out", out]
    cp = subprocess.run(argv, capture_output=True, text=True)
    return json.loads(cp.stdout)          # {ok, chain, frames, layers, transactions, ...}
```

On error, `--json` commands print `{"ok": false, "error": "..."}` and exit `1`, so a
tool can surface `error` directly. For tighter integration, import the same core the
CLI uses (`from hil.drivers._atklogic import AtkCore`) or connect through HIL
(`connect("logic://usb")`). A native HIL MCP server that exposes these primitives as
model tools is on the roadmap (see README).

## 9. Vendoring, provenance & license

- **Location:** `src/hil/_vendor/atklogic/` (`atkproto.py`, `atkdl.py`,
  `srdhost.py`, `LICENSE`, `VENDORED.md`).
- **Upstream:** `https://github.com/hugelzhang/atk-logic-mcp` @ `77320ad`, **MIT**
  ("Copyright (c) 2026 hugelzhang"). Its device protocol was reverse-engineered from
  the vendor `atk-logic` source and hardware-calibrated.
- **Local modifications** (tagged `# HIL-vendor:` in-tree, logged in `VENDORED.md`):
  relative intra-package imports; a platform-aware `_default_decoders_dir()`
  (was a hardcoded Windows path); cross-platform `libusb` discovery in
  `setup_backend()` (was a bundled Windows DLL only).
- **License compatibility:** MIT ↔ MIT — HIL's core stays MIT with **no GPL
  boundary**. `pyusb` (BSD) and `libusb` (LGPL) are optional runtime deps, not
  vendored.

See `src/hil/_vendor/atklogic/VENDORED.md` for the full attribution, the
modification log and the re-sync recipe.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `pyusb/libusb backend unavailable` | install `libusb-1.0` (`brew install libusb` / `apt install libusb-1.0-0-dev`), or set `ATK_LIBUSB_DLL` |
| `cannot open device 1A86:FFCC` | check the USB cable/port; ensure ATK-Logic (or another capture) isn't holding the device; on Linux add a udev rule for `1a86:ffcc` and re-plug |
| `DeviceOccupied` / single-instance guard | close the other process, or set `ATK_ALLOW_MULTI` (at your own risk) |
| `list`/`decode` finds no decoders | set `ATK_DECODERS_DIR` (or `--decoders`) to a sigrok PD tree; the ATK-Logic desktop app ships one |
| decode: `no embedded decoder config` | the `.atkdl` was captured outside the GUI — decode manually with `--decoder NAME --map sig=ch` |
| decode: `sample rate unknown` | use an `.atkdl` (carries the rate) or a `.csv` with a `time_s` column, not a raw `.bin` |
