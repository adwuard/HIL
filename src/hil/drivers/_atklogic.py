"""In-process ATK-Logic-Analyzer backend built on the vendored ``atk-logic-mcp`` core.

The ALIENTEK ATK-Logic-Analyzer (USB ``VID:PID 1A86:FFCC``) is driven *directly*
through the vendored MIT modules :mod:`hil._vendor.atklogic` — ``atkproto`` (device
layer), ``atkdl`` (vendor capture container) and ``srdhost`` (pure-Python sigrok
decoder host). There is **no capture daemon and no GPL boundary**, and because it
is pure Python there is nothing to cross-compile.

Two things live here:

* :class:`AtkCore` — a thin, HIL-owned wrapper over the vendored device layer and
  the single source of truth for identify / capture / save / load / decode / list /
  convert. The ``hil atklogic`` CLI group (and its standalone
  ``python -m hil.drivers._atklogic`` shim) reuse it verbatim, so the CLI and the
  HIL driver can never drift apart.
* :class:`AtkLogicBackend` — adapts :class:`AtkCore` to HIL's
  :class:`~hil.drivers.logic.LogicBackend` protocol so the analyzer flows through
  the existing :class:`~hil.drivers.logic.LogicInstrument` facade, the bench
  ``logic``/``decode`` steps, the pseudo-SCPI set and the ``hil`` CLI unchanged.

Addressing: ``logic://usb`` (optionally ``?serial=..&decoders=..&libusb=..``)
selects this in-process backend; ``logic://host:port`` still selects the JSON-RPC
daemon backend. pyusb + libusb are imported only when a device is first opened, so
importing HIL never requires them.

Capture formats: the pure-Python core cannot emit sigrok ``srzip``, so HIL's
default ``srzip`` is served as the vendor ``.atkdl`` container (openable in
ATK-Logic *and* the natural decode source). ``csv`` (HIL-compatible ``ch<N>``
columns) and ``raw`` (packed primary-channel bytes) are also supported.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactSpool, new_id
from ..errors import HilError

_DEFAULT_THRESHOLD_V = 1.5
_DEFAULT_TRIGGER = "rising"
_DEFAULT_DEPTH = 100_000
_DEFAULT_RATE_HZ = 1_000_000.0
_MAX_DECODE_SAMPLES = 4_000_000

# HIL fetch formats -> what the in-process core actually writes.
_FMT_ALIAS = {"srzip": "atkdl", "": "atkdl", "atkdl": "atkdl",
              "csv": "csv", "raw": "raw", "bin": "raw"}


def _vendored() -> tuple[Any, Any, Any]:
    """Lazy-import the vendored core (stdlib-only at import; pyusb stays lazy)."""
    try:
        from .._vendor.atklogic import atkdl, atkproto, srdhost
    except Exception as exc:  # pragma: no cover - the vendored package always ships
        raise HilError(f"ATK-Logic vendored core import failed: {exc}") from exc
    return atkproto, atkdl, srdhost


def _snap_rate_mhz(sample_rate_hz: float, rates: list[int]) -> int:
    """Snap a Hz sample rate to the device's nearest allowed MHz gear."""
    if not rates:
        return 1
    want = float(sample_rate_hz or _DEFAULT_RATE_HZ) / 1e6
    return min(rates, key=lambda r: (abs(r - want), r))


def _trigger_str(trigger: Any) -> str:
    """Map a HIL trigger dict (channel/level/edge) or word to atkproto's trigger."""
    if isinstance(trigger, str) and trigger.strip():
        return trigger.strip().lower()
    if not isinstance(trigger, dict) or not trigger:
        return _DEFAULT_TRIGGER
    edge = str(trigger.get("edge", "") or "").lower()
    if edge in ("rising", "rise", "r"):
        return "rising"
    if edge in ("falling", "fall", "f"):
        return "falling"
    if edge in ("any", "both", "change", "x"):
        return "any"
    level = trigger.get("level")
    if level in (1, "1", True):
        return "high"
    if level in (0, "0", False):
        return "low"
    return _DEFAULT_TRIGGER


def _mode_str(mode: Any) -> str:
    """HIL 'block'/'buffer' -> device 'buffer'; 'trigger' -> 'trigger'."""
    return "trigger" if str(mode or "buffer").lower() == "trigger" else "buffer"


# -- a completed acquisition ------------------------------------------------
class Capture:
    """The device stream from one acquisition plus the params it was taken with."""

    def __init__(self, core: "AtkCore", res: dict, chs: list[int], depth: int,
                 rate_mhz: int, threshold_v: float, trigger: str, mode: str) -> None:
        self._core = core
        self.res = res
        self.stream = res["stream"]
        self.chs = list(chs)
        self.depth = int(depth)
        self.rate_mhz = int(rate_mhz)
        self.srate_hz = float(rate_mhz) * 1e6
        self.threshold_v = float(threshold_v)
        self.trigger = trigger
        self.mode = mode
        self.complete = bool(res.get("complete"))
        self.elapsed_s = float(res.get("elapsed_s") or 0.0)
        self.trig_offset = getattr(self.stream, "trig_offset", None)

    @property
    def duration_s(self) -> float:
        return self.depth / self.srate_hz if self.srate_hz else 0.0

    def samples(self, ch: int) -> list[int]:
        s, _ = self._core.atkproto.samples_of(self.stream, ch, self.depth, self.srate_hz)
        return s

    def stats(self, ch: int) -> dict:
        return self._core.atkproto.waveform_stats(self.samples(ch), self.srate_hz)

    def channel_bytes(self, ch: int) -> bytes:
        return self.stream.channel_bytes(ch, self.depth)

    def save(self, fmt: str, path: str | Path) -> Path:
        return self._core.save_capture(self, fmt, path)


# -- the HIL-owned device wrapper ------------------------------------------
class AtkCore:
    """identify / capture / save / load / decode over the vendored device layer.

    ``dev`` is an injection seam: tests (and the CLI's offline paths) may pass a
    fake device exposing ``call/acquire/close``; when omitted, a real
    ``atkproto.AtkDevice`` is opened lazily on first use.
    """

    def __init__(self, serial: str | None = None, decoders_dir: str | None = None,
                 libusb: str | None = None, dev: Any | None = None) -> None:
        self.atkproto, self.atkdl, self.srdhost = _vendored()
        self.serial = serial or None
        self.decoders_dir = (decoders_dir or os.environ.get("ATK_DECODERS_DIR")
                             or self.srdhost.DEFAULT_DECODERS_DIR)
        self._libusb_hint = libusb or os.environ.get("ATK_LIBUSB_DLL")
        self._dev = dev
        self._injected = dev is not None
        self._backend_done = False
        self.libusb_path: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def setup_backend(self) -> str | None:
        """Point pyusb at a libusb (auto-discovered or ``$ATK_LIBUSB_DLL``)."""
        if not self._backend_done:
            if self._libusb_hint:
                os.environ["ATK_LIBUSB_DLL"] = self._libusb_hint
            try:
                self.libusb_path = self.atkproto.setup_backend()
            except Exception as exc:
                raise HilError(
                    f"ATK-Logic: pyusb/libusb backend unavailable ({exc}). Install with "
                    "`pip install 'hil-harness[usb]'` and ensure libusb-1.0 is present "
                    "(macOS: `brew install libusb`)."
                ) from exc
            self._backend_done = True
        return self.libusb_path

    def _device(self) -> Any:
        if self._dev is None:
            self.setup_backend()
            try:
                self._dev = self.atkproto.AtkDevice()
                self._dev.call(lambda d: d.manufacturer)   # force open + claim now
            except Exception as exc:
                self._dev = None
                raise HilError(
                    f"ATK-Logic: cannot open device 1A86:FFCC ({exc}). Check the USB "
                    "connection and that no other process holds the analyzer."
                ) from exc
        return self._dev

    def close(self) -> None:
        if self._dev is not None and not self._injected:
            try:
                self._dev.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass
        self._dev = None if not self._injected else self._dev

    # -- info --------------------------------------------------------------
    def identify(self) -> dict:
        P = self.atkproto
        dev = self._device()
        info = dev.call(lambda d: {"manufacturer": d.manufacturer, "product": d.product,
                                   "serial": d.serial_number or ""})
        return {
            "vendor": info.get("manufacturer") or "ALIENTEK",
            "model": info.get("product") or "ATK-Logic-Analyzer",
            "serial": info.get("serial") or "",
            "usb_id": "%04X:%04X" % (P.VID, P.PID),
            "channels": P.CHANNELS,
            "rates_mhz": list(P.RATES),
            "max_sample_rate_hz": max(P.RATES) * 1_000_000,
            "libusb": self.libusb_path,
            "decoders_dir": self.decoders_dir,
        }

    def list_protocols(self, filter: str = "") -> list[str]:
        try:
            names = self.srdhost.list_decoders(self.decoders_dir)
        except (FileNotFoundError, NotADirectoryError):
            return []
        if filter:
            names = [n for n in names if filter.lower() in n.lower()]
        return sorted(names)

    # -- capture -----------------------------------------------------------
    def _chan_indices(self, channels: Any) -> list[int]:
        P = self.atkproto
        raw = channels if isinstance(channels, (list, tuple)) else [channels]
        out: list[int] = []
        for c in raw:
            idx = P.channel_to_index(c)
            if not 0 <= idx < P.CHANNELS:
                raise HilError(f"ATK-Logic: channel out of range 0..{P.CHANNELS - 1}: {c}")
            if idx not in out:
                out.append(idx)
        return out or [0]

    def capture(self, channels: Any = (0,), depth: int = _DEFAULT_DEPTH,
                sample_rate_hz: float = _DEFAULT_RATE_HZ,
                threshold_v: float = _DEFAULT_THRESHOLD_V, trigger: Any = _DEFAULT_TRIGGER,
                mode: str = "buffer", timeout_s: float | None = None,
                rle: bool = False) -> Capture:
        """One blocking acquisition; returns a :class:`Capture` (all 16 ch are read)."""
        P = self.atkproto
        chs = self._chan_indices(channels)
        rate_mhz = _snap_rate_mhz(sample_rate_hz, list(P.RATES))
        depth = max(8, int(depth) - int(depth) % 8)
        trig = _trigger_str(trigger)
        dev = self._device()
        try:
            res = dev.acquire(rate_mhz=rate_mhz, depth=depth, threshold_v=float(threshold_v),
                              trigger=trig, channel=chs[0], mode=_mode_str(mode),
                              timeout_s=timeout_s, extra_channels=chs[1:], rle=bool(rle))
        except HilError:
            raise
        except Exception as exc:
            raise HilError(f"ATK-Logic: acquire failed ({exc})") from exc
        return Capture(self, res, chs, depth, rate_mhz, threshold_v, trig, _mode_str(mode))

    # -- save --------------------------------------------------------------
    def save_capture(self, cap: Capture, fmt: str, path: str | Path) -> Path:
        fmt = _FMT_ALIAS.get((fmt or "atkdl").lower(), (fmt or "atkdl").lower())
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "atkdl":
            self.atkdl.stream_to_atkdl(cap.stream, cap.chs, cap.rate_mhz, cap.depth, str(p),
                                       threshold_v=cap.threshold_v, trigger=cap.trigger,
                                       mode=cap.mode)
        elif fmt == "csv":
            self._write_csv(cap, p)
        elif fmt == "raw":
            p.write_bytes(cap.channel_bytes(cap.chs[0]))
        else:
            raise HilError(f"ATK-Logic: unsupported capture format '{fmt}' (use atkdl|csv|raw)")
        return p

    def _write_csv(self, cap: Capture, p: Path) -> None:
        # HIL-compatible columns (index,time_s,ch<N>...) so LogicInstrument's
        # bounded capture() path and the sim/daemon CSVs stay interchangeable.
        cols = {c: cap.samples(c) for c in cap.chs}
        srate = cap.srate_hz or 1.0
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["index", "time_s"] + [f"ch{c}" for c in cap.chs])
            for i in range(cap.depth):
                w.writerow([i, f"{i / srate:.9g}"]
                           + [(cols[c][i] if i < len(cols[c]) else 0) for c in cap.chs])

    # -- load (offline) ----------------------------------------------------
    def load_samples(self, path: str | Path, channels: Any = None,
                     max_samples: int = _MAX_DECODE_SAMPLES) -> tuple[dict[int, list[int]], float, dict]:
        """Return ``(samples_by_hw, sample_rate_hz, info)`` from a saved artifact."""
        p = Path(path)
        if not p.exists():
            raise HilError(f"ATK-Logic: artifact not found: {path}")
        suffix = p.suffix.lower()
        if suffix == ".atkdl":
            return self._load_atkdl(p, channels, max_samples)
        if suffix == ".csv":
            return self._load_csv(p, channels, max_samples)
        if suffix in (".bin", ".raw"):
            raise HilError("ATK-Logic: decoding a raw .bin needs a sample rate — "
                           "re-capture as .atkdl (default) or .csv")
        raise HilError(f"ATK-Logic: cannot load samples from a '{suffix}' artifact")

    def _load_atkdl(self, p: Path, channels: Any, max_samples: int):
        s = self.atkdl.read_atkdl(str(p))
        srate = float(getattr(s, "sample_rate_hz", 0.0) or 0.0)
        try:
            active = list(s.active_channels() or [])
        except Exception:  # pragma: no cover - tolerate older containers
            active = []
        chs = self._chan_indices(channels) if channels is not None else (active or [0])
        out: dict[int, list[int]] = {}
        for c in chs:
            total = 0
            try:
                total = int(s.valid_count(c) or 0)
            except Exception:
                total = 0
            if not total:
                total = len(s.raw_bytes(c) or b"") * 8
            n = min(int(max_samples), total) if total else int(max_samples)
            out[c] = s.window(c, 0, n)
        return out, srate, {"source": str(p), "format": "atkdl", "active_channels": active}

    def _load_csv(self, p: Path, channels: Any, max_samples: int):
        colmap: dict[int, int] = {}
        tidx: int | None = None
        rows: list[list[str]] = []
        with p.open(newline="", encoding="utf-8") as fh:
            line = fh.readline()
            while line.startswith("#"):                 # tolerate a comment banner
                line = fh.readline()
            header = [h.strip().lower() for h in line.rstrip("\r\n").split(",")]
            for i, h in enumerate(header):
                if h.startswith("ch"):
                    try:
                        colmap[i] = int(h[2:])
                    except ValueError:
                        pass
            if "time_s" in header:
                tidx = header.index("time_s")
            for line in fh:
                rows.append(line.rstrip("\r\n").split(","))
                if len(rows) >= int(max_samples):
                    break
        srate = 0.0
        if tidx is not None and len(rows) >= 2:
            try:
                dt = float(rows[1][tidx]) - float(rows[0][tidx])
                srate = (1.0 / dt) if dt > 0 else 0.0
            except (ValueError, IndexError):
                srate = 0.0
        want = self._chan_indices(channels) if channels is not None else sorted(colmap.values())
        out: dict[int, list[int]] = {}
        for c in want:
            cidx = next((i for i, cc in colmap.items() if cc == c), None)
            if cidx is None:
                out[c] = [0] * len(rows)
                continue
            col: list[int] = []
            for r in rows:
                try:
                    col.append(int(float(r[cidx])))
                except (ValueError, IndexError):
                    col.append(0)
            out[c] = col
        return out, srate, {"source": str(p), "format": "csv", "rows": len(rows)}

    # -- decode ------------------------------------------------------------
    def decode(self, path: str | Path, decoders: list[dict] | None = None,
               max_samples: int = _MAX_DECODE_SAMPLES, max_items: int = 200) -> dict:
        """Decode a saved artifact with the vendored sigrok PDs (offline)."""
        srd = self.srdhost
        dd = self.decoders_dir
        decoders = decoders or []
        if not decoders and str(path).lower().endswith(".atkdl"):
            try:                                   # use the file's official config
                chain, layers = srd.from_atkdl(str(path), decoders_dir=dd,
                                               max_samples=int(max_samples),
                                               annotation_limit=int(max_items))
            except Exception as exc:
                raise HilError(
                    f"ATK-Logic decode from the .atkdl's embedded config failed ({exc}). "
                    "If this capture has no saved decoder config, decode manually with "
                    "--decoder NAME [--map sig=ch] (see 'hil atklogic list')."
                ) from exc
            frames = sum(int(l.get("annotation_total", 0) or 0) for l in (layers or []))
            return {"chain": chain, "layers": layers, "transactions": None,
                    "source": str(path), "from_saved": True, "frames": frames}
        if not decoders:
            raise HilError("ATK-Logic decode needs at least one decoder name "
                           "(or an .atkdl with an official decoder config)")
        names = [ (d.get("name") if isinstance(d, dict) else str(d)) for d in decoders ]
        names = [n for n in names if n]
        try:
            chain = [srd.resolve(n, dd) for n in names]
        except Exception as exc:
            raise HilError(f"ATK-Logic decode: cannot resolve decoder ({exc})") from exc
        first = decoders[0] if isinstance(decoders[0], dict) else {}
        cmap = {str(k): int(v) for k, v in (first.get("channels") or first.get("map") or {}).items()}
        opts_map: dict[str, dict] = {}
        for d, dir_name in zip(decoders, chain):
            if isinstance(d, dict) and d.get("options"):
                opts_map[dir_name] = {str(k): v for k, v in d["options"].items()}
        hw = sorted(set(cmap.values())) or None
        samples, srate, info = self.load_samples(path, channels=hw, max_samples=max_samples)
        if not srate:
            raise HilError("ATK-Logic decode: sample rate unknown (use an .atkdl artifact)")
        try:
            results = srd.run_chain(chain, {chain[0]: cmap} if cmap else {}, opts_map,
                                    srate, samples, dd, max_samples=int(max_samples))
            layers = srd.summarize(results, max_items=int(max_items))
            txns = srd.transactions(results, max_items=int(max_items))
        except Exception as exc:
            raise HilError(f"ATK-Logic decode failed: {exc}") from exc
        return {"chain": chain, "layers": layers, "transactions": txns,
                "source": str(path), "from_saved": False, "sample_rate_hz": srate,
                "window": info, "frames": int((txns or {}).get("total", 0))}

    def write_decode(self, result: dict, fmt: str, path: str | Path) -> tuple[Path, int]:
        """Serialize a :meth:`decode` result to CSV/JSON. Returns ``(path, frames)``."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fmt = (fmt or "csv").lower()
        rows = _decode_rows(result)
        frames = int(result.get("frames") or len(rows))
        if fmt == "json":
            p.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
        else:
            with p.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["sample", "t_ms", "protocol", "row", "text"])
                for r in rows:
                    w.writerow([r.get("sample", ""), r.get("t_ms", ""),
                                r.get("protocol", ""), r.get("row", ""), r.get("text", "")])
        return p, frames

    # -- convert (offline: re-serialize a saved artifact) ------------------
    def convert(self, src: str | Path, fmt: str, path: str | Path,
                channels: Any = None) -> dict:
        """Re-serialize a saved ``.atkdl``/``.csv`` capture to ``csv`` or ``raw``.

        Pure-Python and offline: loads samples via :meth:`load_samples` then writes
        the requested container. ``raw`` packs the primary (first) channel LSB-first
        via the vendored ``atkdl.pack_samples``.
        """
        samples, srate, info = self.load_samples(src, channels=channels)
        fmt = (fmt or "csv").lower()
        if fmt == "bin":
            fmt = "raw"
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        chs = sorted(samples.keys()) or [0]
        n = max((len(samples.get(c, [])) for c in chs), default=0)
        if fmt == "csv":
            denom = srate or 1.0
            with p.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["index", "time_s"] + [f"ch{c}" for c in chs])
                for i in range(n):
                    w.writerow([i, f"{i / denom:.9g}"]
                               + [(samples[c][i] if i < len(samples.get(c, [])) else 0)
                                  for c in chs])
        elif fmt == "raw":
            p.write_bytes(self.atkdl.pack_samples(samples.get(chs[0], [])))
        else:
            raise HilError(f"ATK-Logic convert: unsupported target format '{fmt}' (use csv|raw)")
        return {"path": str(p), "format": fmt, "channels": chs, "sample_rate_hz": srate,
                "sample_count": n, "bytes": p.stat().st_size if p.exists() else 0,
                "source": str(src), "source_info": info}


def _decode_rows(result: dict) -> list[dict]:
    """Flatten a decode result into time-ordered rows (transactions, else annotations)."""
    txns = result.get("transactions")
    if isinstance(txns, dict) and txns.get("items") is not None:
        return list(txns["items"])
    rows: list[dict] = []
    for layer in (result.get("layers") or []):
        proto = layer.get("protocol", "")
        for ann in (layer.get("annotations") or {}).values():
            rowname = ann.get("row", "")
            for it in (ann.get("items") or []):
                t = it.get("t") or [None, None]
                text = it.get("text")
                rows.append({"sample": t[0] if isinstance(t, list) else None,
                             "t_ms": "", "protocol": proto, "row": rowname,
                             "text": " ".join(text) if isinstance(text, (list, tuple)) else str(text or "")})
    return rows


# -- the HIL LogicBackend implementation -----------------------------------
class AtkLogicBackend:
    """In-process :class:`~hil.drivers.logic.LogicBackend` for ``logic://usb``.

    The device acquisition is synchronous (``atkproto.acquire`` blocks and returns
    the full stream), so :meth:`arm` performs the capture and caches it; ``wait``
    and ``status`` then report ``done`` immediately and :meth:`fetch` serializes
    the cached stream. ``core`` is an injection seam for hardware-free tests.
    """

    def __init__(self, serial: str | None = None, decoders_dir: str | None = None,
                 libusb: str | None = None, spool: ArtifactSpool | None = None,
                 timeout: float = 5.0, core: AtkCore | None = None) -> None:
        self.core = core or AtkCore(serial=serial, decoders_dir=decoders_dir, libusb=libusb)
        self.spool = spool or ArtifactSpool()
        self.timeout = timeout
        self._cfg: dict[str, Any] = {}
        self._caps: dict[str, Capture] = {}
        self._cid = ""
        self._state = "idle"

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self.core.setup_backend()          # prepare libusb; does not claim the device

    def close(self) -> None:
        try:
            self.core.close()
        except Exception:  # pragma: no cover - close must never raise
            pass
        self._state = "idle"

    def info(self) -> str:
        return f"atk-logic@usb" + (f":{self.core.serial}" if self.core.serial else "")

    # -- identity ----------------------------------------------------------
    def hello(self) -> dict:
        ident = self.core.identify()
        return {
            "protocol": "1",
            "device": {"vendor": ident["vendor"], "model": ident["model"],
                       "serial": ident["serial"], "variant": "usb-inprocess"},
            "channels": ident["channels"],
            "max_sample_rate_hz": ident["max_sample_rate_hz"],
            "decoders": self.core.list_protocols()[:64],
            "spool_dir": str(self.spool.root),
            "busy": False,
            "usb_id": ident["usb_id"],
            "rates_mhz": ident["rates_mhz"],
            "decoders_dir": ident["decoders_dir"],
        }

    # -- capture flow ------------------------------------------------------
    def configure(self, config: dict) -> dict:
        self._cfg = dict(config or {})
        return {"ok": True, "config": self._cfg}

    def _capture_params(self) -> dict:
        cfg = self._cfg
        rate = float(cfg.get("sample_rate_hz") or _DEFAULT_RATE_HZ)
        depth = cfg.get("depth_samples")
        if not depth:
            duration = float(cfg.get("duration_s") or 0.0)
            depth = int(rate * duration) if duration else _DEFAULT_DEPTH
        return {
            "channels": cfg.get("channels") or [0],
            "depth": int(depth),
            "sample_rate_hz": rate,
            "threshold_v": float(cfg.get("threshold_v") or _DEFAULT_THRESHOLD_V),
            "trigger": cfg.get("trigger") or _DEFAULT_TRIGGER,
            "mode": cfg.get("mode") or "buffer",
        }

    def arm(self) -> str:
        params = self._capture_params()
        cap = self.core.capture(timeout_s=max(self.timeout, 30.0), **params)
        cid = new_id("cap")
        self._caps[cid] = cap
        self._cid = cid
        self._state = "done" if cap.complete else "partial"
        return cid

    def status(self, capture_id: str = "") -> dict:
        cap = self._caps.get(capture_id or self._cid)
        return {"state": self._state, "progress": 1.0 if cap is not None else 0.0,
                "samples_captured": cap.depth if cap else 0,
                "complete": cap.complete if cap else False}

    def wait(self, capture_id: str = "", timeout_s: float = 30.0) -> dict:
        return self.status(capture_id)     # the capture already completed in arm()

    def _cap(self, capture_id: str = "") -> Capture:
        cap = self._caps.get(capture_id or self._cid)
        if cap is None:
            raise HilError("ATK-Logic: no capture to fetch — arm first")
        return cap

    def fetch(self, capture_id: str = "", fmt: str = "srzip",
              dest: str = "spool", path: str | None = None) -> dict:
        cap = self._cap(capture_id)
        real = _FMT_ALIAS.get((fmt or "atkdl").lower(), (fmt or "atkdl").lower())
        if dest == "path" and path:
            p = Path(path)
            if p.suffix == "":
                p = p.with_suffix(".atkdl" if real == "atkdl" else f".{real}")
        else:
            p = self.spool.new_path("logic", real)
        cap.save(real, p)
        return {
            "id": new_id("logic"), "kind": "logic", "path": str(p), "format": real,
            "channels": cap.chs, "sample_rate_hz": cap.srate_hz,
            "sample_count": cap.depth, "duration_s": cap.duration_s,
            "bytes": p.stat().st_size if p.exists() else 0,
            "trigger": {"edge": cap.trigger, "channel": cap.chs[0]},
            "complete": cap.complete, "elapsed_s": cap.elapsed_s,
            "trigger_offset_samples": cap.trig_offset,
        }

    def decode(self, artifact_path: str, decoders: list[dict],
               fmt: str = "csv", out: str | None = None) -> dict:
        result = self.core.decode(artifact_path, decoders)
        p = Path(out) if out else self.spool.new_path("decoded", fmt or "csv")
        p, frames = self.core.write_decode(result, fmt or "csv", p)
        names = [d.get("name", "") if isinstance(d, dict) else str(d) for d in (decoders or [])]
        return {
            "id": new_id("dec"), "kind": "decoded", "path": str(p),
            "format": (fmt or "csv").lower(), "frames": frames,
            "decoders": [n for n in names if n] or list(result.get("chain") or []),
            "bytes": p.stat().st_size if p.exists() else 0,
        }

    def stop(self, capture_id: str = "") -> None:
        try:
            if self.core._dev is not None:
                self.core._dev.stop()
        except Exception:  # pragma: no cover - stop is best-effort
            pass
        self._state = "idle"


# -- standalone entry point ---------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """``python -m hil.drivers._atklogic ...`` — delegates to ``hil atklogic ...``.

    A standalone entry point (handy for embedding / MCP without the ``hil`` console
    script) that shares one argparse surface: the real parser lives in :mod:`hil.cli`,
    so this can never drift from ``hil atklogic``.
    """
    import sys as _sys

    from ..cli import main as _hil_main

    rest = list(_sys.argv[1:] if argv is None else argv)
    return _hil_main(["atklogic", *rest])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
