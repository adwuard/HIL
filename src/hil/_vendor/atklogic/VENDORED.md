# Vendored: atk-logic-mcp core

HIL vendors three modules from **atk-logic-mcp** so the ATK-Logic-Analyzer can be
driven **in-process** (no daemon, no GPL boundary, pure Python → no cross-compiling).

| | |
|---|---|
| **Upstream** | https://github.com/hugelzhang/atk-logic-mcp |
| **Commit** | `77320addfb34d3973d09a7af663de76023034587` (2026-09-13) |
| **License** | MIT — Copyright (c) 2026 hugelzhang (see [`LICENSE`](./LICENSE)) |
| **Device** | ALIENTEK ATK-Logic-Analyzer, USB VID:PID `1A86:FFCC`, 16 ch, ≤200 MS/s |
| **Protocol source** | Reverse-engineered by upstream from vendor source `github.com/alientek-openedv/atk-logic` (`pv/usb/usb_control.cpp`, `pv/static/util.cpp`) + real-hardware calibration |

HIL is MIT-licensed too, so vendoring is license-compatible. `LICENSE` is kept
verbatim alongside the code as required by the MIT terms.

## Vendored files

| File | Role | Imported from |
|---|---|---|
| `atkproto.py` | USB protocol + device layer (`AtkDevice`, `setup_backend`, `acquire`, `waveform_stats`, …) | — (base; pyusb imported lazily) |
| `atkdl.py` | Vendor `.atkdl` capture container read/write | `atkproto` |
| `srdhost.py` | Pure-Python `libsigrokdecode` host (runs the vendor's 200+ sigrok PDs) | `atkdl` (lazy) |

**Not** vendored: upstream `server.py` (FastMCP server — HIL exposes its own CLI +
`LogicBackend` instead), tests, `libusb-1.0.dll` (Windows binary), `docs/`,
`tools/`, `drivers/`, `README.md`, `PROTOCOL-NOTES.md`, `TEST-REPORT.md`.

## Local modifications

All edits are minimal and tagged `# HIL-vendor:` in-source so they can be
re-applied when re-syncing from upstream.

1. **`atkdl.py`** — flat `import atkproto as P` → relative `from . import atkproto as P`
   (so the module resolves inside the `hil._vendor.atklogic` package).
2. **`srdhost.py`** — lazy `import atkdl as _atkdl` (in `run_from_atkdl`) → relative
   `from . import atkdl as _atkdl`.
3. **`srdhost.py`** — `DEFAULT_DECODERS_DIR` was hardcoded to the Windows install path
   `C:\Program Files\ATK-Logic\decoders`. Replaced with a platform-aware resolver
   `_default_decoders_dir()` that honors `$ATK_DECODERS_DIR` first, then known
   macOS/Linux/Windows locations, picking the first that exists. Callers may still
   pass `decoders_dir=` explicitly.
4. **`atkproto.py`** — `setup_backend()` only looked for a bundled Windows
   `libusb-1.0.dll`. Extended to also probe the platform soname
   (`libusb-1.0.dylib` / `libusb-1.0.so[.0]`) in the package/extra dirs plus common
   Homebrew and system prefixes. `$ATK_LIBUSB_DLL` still takes highest priority, and
   the Windows `.dll` behavior is unchanged. Falls back to pyusb's system lookup
   (returns `None`) when nothing is found.

## Re-syncing from upstream

```sh
git clone https://github.com/hugelzhang/atk-logic-mcp /tmp/atk
cp /tmp/atk/atk-logic/{atkproto,atkdl,srdhost}.py src/hil/_vendor/atklogic/
cp /tmp/atk/LICENSE src/hil/_vendor/atklogic/
# then re-apply modifications 1–4 above (grep for "# HIL-vendor:")
```

## Runtime prerequisites (not vendored)

- **pyusb** — HIL's `usb` extra (`pip install 'hil-harness[usb]'`).
- **libusb 1.0** — macOS: `brew install libusb`; Linux: `apt install libusb-1.0-0`.
- **sigrok protocol decoders** — the vendor PD directory (208 decoders). Point
  `$ATK_DECODERS_DIR` at it, or install ATK-Logic to a known location. Without a
  decoder directory, capture/PWM still work; only protocol decoding is unavailable.
