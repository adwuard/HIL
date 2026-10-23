"""Logic-analyzer driver — a streaming, file-based, non-SCPI instrument.

A logic analyzer is unlike the SCPI / PPK2 devices HIL started with: it produces
*huge*, timing-critical captures (MBs-GBs), needs hardware triggers, and its
value is in *protocol decoding*. Loading that into memory as ``Sample`` records
would blow up, and the timing-critical USB streaming must not run in HIL's
control loop.

So the split is:

* A **capture daemon** (out of process, GPL) owns the USB device, does the
  real-time libusb streaming and the ``libsigrokdecode`` decoding, and writes
  captures to a spool on disk. For the ALIENTEK ATK-Logic this daemon is a
  headless ``--server`` build of the ``atk-logic`` Qt app; see
  ``docs/logic-daemon-protocol.md`` for the JSON-RPC contract.
* This **MIT client driver** keeps a persistent connection to that daemon and
  drives it over newline-delimited **JSON-RPC 2.0 on TCP** (``logic://host:port``).
  Captures come back as :class:`~hil.artifacts.CaptureArtifact` file references,
  never as bulk bytes in memory.

Like the ``ppk2`` driver, the backend is a small swappable
:class:`LogicBackend` protocol: :class:`LogicDaemonBackend` talks to the real
daemon, :class:`LogicSimBackend` synthesizes captures so bench runs and CI work
with no hardware, and tests can inject a fake.

A curated **pseudo-SCPI facade** lets the analyzer reuse the rest of HIL
unchanged (registry safety policy, bench procedures, ``--sim`` dry-runs, CLI)::

    *IDN?  :CONF:CHAN <n|list>  :CONF:RATE <hz>  :CONF:DEPTH <n>  :CONF:DUR <s>
    :CONF:FORM <csv|srzip|raw>  :TRIG:CHAN <n>  :TRIG:LEV <0|1>  :TRIG:EDGE <R|F>
    :TRIG:POS <pct>  :ACQ:ARM (:INIT:ACQ)  :ACQ:WAIT  :ACQ:STOP (:MEAS:STOP)
    :ACQ:STAT?  :FETCh?  :DEC <name>?

The bench ``logic`` / ``decode`` steps call the richer :meth:`LogicInstrument.run_capture`
/ :meth:`LogicInstrument.decode` directly (duck-typed), which return artifacts.
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

from ..addresses import DEFAULT_LOGIC_PORT, Address
from ..artifacts import ArtifactSpool, CaptureArtifact, new_id
from ..errors import HilError, TimeoutError_
from ..instrument import BaseInstrument
from ..registry import Device
from ..telemetry import Sample
from ..transports.tcp import TcpTransport

_DEFAULT_RATE = 1_000_000.0
_CAPTURE_ROW_CAP = 512          # rows the compatibility capture() path reads back
_EDGE_MAP = {"R": "rising", "RISE": "rising", "RISING": "rising",
             "F": "falling", "FALL": "falling", "FALLING": "falling"}


# -- backend protocol -------------------------------------------------------
class LogicBackend(Protocol):
    """The minimal, swappable surface a logic-analyzer backend must provide."""

    def open(self) -> None: ...
    def hello(self) -> dict: ...
    def configure(self, config: dict) -> dict: ...
    def arm(self) -> str: ...                                    # -> capture_id
    def status(self, capture_id: str = "") -> dict: ...
    def wait(self, capture_id: str = "", timeout_s: float = 30.0) -> dict: ...
    def fetch(self, capture_id: str = "", fmt: str = "srzip",
              dest: str = "spool", path: str | None = None) -> dict: ...  # -> artifact dict
    def decode(self, artifact_path: str, decoders: list[dict],
               fmt: str = "csv", out: str | None = None) -> dict: ...     # -> decoded artifact dict
    def stop(self, capture_id: str = "") -> None: ...
    def close(self) -> None: ...
    def info(self) -> str: ...


# -- helpers ----------------------------------------------------------------
def _norm_decoder(d: Any) -> dict:
    if isinstance(d, str):
        return {"name": d.strip()}
    if isinstance(d, dict):
        name = d.get("name") or d.get("decoder") or ""
        return {
            "name": str(name),
            "options": dict(d.get("options", {}) or {}),
            "channels": d.get("channels", d.get("map", {})) or {},
        }
    return {"name": str(d)}


def _norm_decoders(decoders: Any) -> list[dict]:
    if decoders is None:
        return []
    if isinstance(decoders, (str, dict)):
        return [_norm_decoder(decoders)]
    return [_norm_decoder(d) for d in decoders]


def _chan_list(channels: Any, default: int = 8) -> list[int]:
    """Normalize a channel spec (count int, or list/CSV of indices) to [int]."""
    if channels is None:
        return list(range(default))
    if isinstance(channels, bool):
        return list(range(default))
    if isinstance(channels, int):
        return list(range(channels))
    if isinstance(channels, (list, tuple)):
        return [int(c) for c in channels]
    if isinstance(channels, str):
        s = channels.strip()
        if not s:
            return list(range(default))
        if re.fullmatch(r"\d+", s):
            n = int(s)
            # A bare small integer is a count; a lone big index is still a count.
            return list(range(n))
        return [int(x) for x in re.split(r"[,\s]+", s) if x.strip()]
    return list(range(default))


# -- daemon backend (JSON-RPC over TCP) ------------------------------------
class LogicDaemonBackend:
    """Client for the ATK-Logic capture daemon (newline-delimited JSON-RPC 2.0).

    Reuses :class:`~hil.transports.tcp.TcpTransport` for the byte-pipe (it frames
    on ``\\n`` and appends the terminator on write), so the persistent session,
    timeouts and reconnect all live in one place. Large capture data never flows
    through here: ``fetch`` asks the daemon to write to its spool (or a path) and
    returns only artifact *metadata*.
    """

    def __init__(self, host: str, port: int = DEFAULT_LOGIC_PORT,
                 timeout: float = 5.0, transport: Any | None = None) -> None:
        self.host = host
        self.port = port or DEFAULT_LOGIC_PORT
        self.timeout = timeout
        self._transport = transport or TcpTransport(host, self.port, timeout=timeout)
        self._id = 0
        self._hello: dict | None = None

    def open(self) -> None:
        self._transport.open()

    def _call(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        self._id += 1
        rid = self._id
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            req["params"] = params
        self._transport.write(json.dumps(req))
        wait = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError_(f"logic daemon: no response to '{method}' within {wait}s")
            line = (self._transport.read(timeout=remaining) or "").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # ignore banners / non-JSON lines
            if isinstance(msg, dict) and msg.get("id") == rid:
                err = msg.get("error")
                if err:
                    emsg = err.get("message") if isinstance(err, dict) else str(err)
                    raise HilError(f"logic daemon error on '{method}': {emsg}")
                return msg.get("result")
            # A notification or a different id: keep reading until ours arrives.

    def hello(self) -> dict:
        if self._hello is None:
            self._hello = self._call("hello") or {}
        return self._hello

    def configure(self, config: dict) -> dict:
        return self._call("configure", config) or {}

    def arm(self) -> str:
        res = self._call("arm") or {}
        return str(res.get("capture_id") or res.get("id") or new_id("cap"))

    def status(self, capture_id: str = "") -> dict:
        return self._call("status", {"capture_id": capture_id}) or {}

    def wait(self, capture_id: str = "", timeout_s: float = 30.0) -> dict:
        return self._call("wait", {"capture_id": capture_id, "timeout_s": timeout_s},
                          timeout=timeout_s + self.timeout) or {}

    def fetch(self, capture_id: str = "", fmt: str = "srzip",
              dest: str = "spool", path: str | None = None) -> dict:
        params: dict[str, Any] = {"capture_id": capture_id, "format": fmt, "dest": dest}
        if path:
            params["path"] = str(path)
        res = self._call("fetch", params, timeout=max(self.timeout, 60.0)) or {}
        return res.get("artifact", res)

    def decode(self, artifact_path: str, decoders: list[dict],
               fmt: str = "csv", out: str | None = None) -> dict:
        params: dict[str, Any] = {"artifact": str(artifact_path),
                                  "decoders": decoders, "format": fmt}
        if out:
            params["out"] = str(out)
        res = self._call("decode", params, timeout=max(self.timeout, 60.0)) or {}
        return res.get("artifact", res)

    def stop(self, capture_id: str = "") -> None:
        try:
            self._call("stop", {"capture_id": capture_id})
        except HilError:  # pragma: no cover - stop is best-effort
            pass

    def close(self) -> None:
        try:
            self._call("close", timeout=2.0)
        except Exception:  # pragma: no cover - close must never raise
            pass
        try:
            self._transport.close()
        except Exception:  # pragma: no cover
            pass

    def info(self) -> str:
        return f"atk-logic-daemon@{self.host}:{self.port}"


# -- simulator backend -----------------------------------------------------
class LogicSimBackend:
    """Synthesizes captures + decodes so bench runs/CI need no hardware.

    Writes small, deterministic files into an :class:`~hil.artifacts.ArtifactSpool`
    (a binary counter pattern across channels for the waveform; a few frames for
    a decode). File rows are capped so a nominal multi-MSps capture stays tiny.
    """

    def __init__(self, spool: ArtifactSpool | None = None, *, max_rows: int = 4096) -> None:
        self.spool = spool or ArtifactSpool()
        self._max_rows = max_rows
        self._config: dict[str, Any] = {}
        self._cid = ""
        self._state = "idle"

    def open(self) -> None:
        pass

    def hello(self) -> dict:
        return {
            "protocol": "1",
            "device": {"vendor": "ALIENTEK", "model": "ATK-Logic",
                       "serial": "SIM0001", "variant": "sim"},
            "channels": 16,
            "max_sample_rate_hz": 200_000_000,
            "decoders": ["i2c", "spi", "uart", "1wire", "can", "modbus"],
            "spool_dir": str(self.spool.root),
            "busy": False,
        }

    def configure(self, config: dict) -> dict:
        self._config = dict(config or {})
        return {"ok": True, "config": self._config}

    def arm(self) -> str:
        self._cid = new_id("cap")
        self._state = "done"          # the simulator captures instantly
        return self._cid

    def status(self, capture_id: str = "") -> dict:
        return {"state": self._state, "progress": 1.0 if self._state == "done" else 0.0,
                "samples_captured": self._nominal_samples()}

    def wait(self, capture_id: str = "", timeout_s: float = 30.0) -> dict:
        return self.status(capture_id)

    def _nominal_samples(self) -> int:
        rate = float(self._config.get("sample_rate_hz") or _DEFAULT_RATE)
        depth = self._config.get("depth_samples")
        if depth:
            return int(depth)
        duration = float(self._config.get("duration_s") or 0.0)
        return int(rate * duration) if duration else 1024

    def fetch(self, capture_id: str = "", fmt: str = "srzip",
              dest: str = "spool", path: str | None = None) -> dict:
        rate = float(self._config.get("sample_rate_hz") or _DEFAULT_RATE)
        chans = _chan_list(self._config.get("channels"))
        n = self._nominal_samples()
        duration = float(self._config.get("duration_s") or (n / rate if rate else 0.0))
        rows = min(max(1, n), self._max_rows)
        fmt = (fmt or "srzip").lower()
        if dest == "path" and path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p = self.spool.new_path("logic", fmt)
        if fmt == "csv":
            self._write_csv(p, rows, rate, chans)
        else:
            # An opaque stand-in for an srzip/raw capture (not read back by HIL).
            p.write_bytes(b"ATKLOGIC-SIM\x00" + n.to_bytes(8, "little"))
        aid = new_id("logic")
        return {
            "id": aid, "kind": "logic", "path": str(p), "format": fmt,
            "channels": chans, "sample_rate_hz": rate, "sample_count": n,
            "duration_s": duration, "bytes": p.stat().st_size if p.exists() else 0,
            "trigger": dict(self._config.get("trigger", {}) or {}),
        }

    def _write_csv(self, p: Path, rows: int, rate: float, chans: list[int]) -> None:
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["index", "time_s"] + [f"ch{c}" for c in chans])
            for i in range(rows):
                t = i / rate if rate else 0.0
                w.writerow([i, f"{t:.9g}"] + [(i >> (k + 1)) & 1 for k in range(len(chans))])

    def decode(self, artifact_path: str, decoders: list[dict],
               fmt: str = "csv", out: str | None = None) -> dict:
        names = [d.get("name", "?") for d in (decoders or [])]
        p = Path(out) if out else self.spool.new_path("decoded", fmt or "csv")
        p.parent.mkdir(parents=True, exist_ok=True)
        frames = 4
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["frame", "decoder", "field", "value"])
            for i in range(frames):
                w.writerow([i, names[0] if names else "?", "data", f"0x{0x10 + i:02x}"])
        return {
            "id": new_id("dec"), "kind": "decoded", "path": str(p),
            "format": fmt or "csv", "frames": frames, "decoders": names,
            "bytes": p.stat().st_size if p.exists() else 0,
        }

    def stop(self, capture_id: str = "") -> None:
        self._state = "idle"

    def close(self) -> None:
        self._state = "idle"

    def info(self) -> str:
        return "logic-sim"


# -- instrument -----------------------------------------------------------
class LogicInstrument(BaseInstrument):
    """A logic analyzer presented through HIL's :class:`~hil.instrument.Instrument`."""

    def __init__(self, backend: LogicBackend, device: Device | None = None,
                 timeout: float = 5.0, spool: ArtifactSpool | None = None) -> None:
        super().__init__(device)
        self.timeout = timeout
        self.backend: LogicBackend = backend
        self.spool = spool or ArtifactSpool()
        self.backend.open()
        self._hello: dict | None = None
        self._cfg: dict[str, Any] = {}
        self._trigger: dict[str, Any] = {}
        self._fetch_fmt = "srzip"
        self._capture_id = ""
        self._last_artifact: CaptureArtifact | None = None

    # -- identity ---------------------------------------------------------
    def _dev(self) -> dict:
        if self._hello is None:
            try:
                self._hello = self.backend.hello() or {}
            except Exception:  # pragma: no cover - tolerate an offline daemon
                self._hello = {}
        return self._hello

    def idn(self) -> str:
        dev = (self._dev().get("device") or {})
        return (f"{dev.get('vendor', 'ALIENTEK')},{dev.get('model', 'ATK-Logic')},"
                f"{dev.get('serial', '')},{dev.get('variant', self.backend.info())}")

    # -- pseudo-SCPI write ------------------------------------------------
    def write(self, command: str) -> None:
        cmd = self._check(command)
        head, _, arg = cmd.partition(" ")
        head, arg = head.strip().upper(), arg.strip()
        if head == ":CONF:CHAN":
            self._cfg["channels"] = _chan_list(arg)
        elif head == ":CONF:RATE":
            self._cfg["sample_rate_hz"] = float(arg)
        elif head == ":CONF:DEPTH":
            self._cfg["depth_samples"] = int(float(arg))
        elif head in (":CONF:DUR", ":CONF:DURATION"):
            self._cfg["duration_s"] = float(arg)
        elif head == ":CONF:FORM":
            self._fetch_fmt = arg.lower()
        elif head == ":TRIG:CHAN":
            self._trigger["channel"] = int(float(arg))
        elif head == ":TRIG:LEV":
            self._trigger["level"] = int(float(arg))
        elif head == ":TRIG:EDGE":
            self._trigger["edge"] = _EDGE_MAP.get(arg.upper(), arg.lower())
        elif head == ":TRIG:POS":
            self._trigger["position_pct"] = float(arg)
        elif head == ":TRIG:OFF":
            self._trigger = {}
        elif head in (":ACQ:ARM", ":INIT:ACQ", ":INIT"):
            self.backend.configure(self._merged())
            self._capture_id = self.backend.arm()
        elif head == ":ACQ:WAIT":
            self.backend.wait(self._capture_id, self.timeout)
        elif head in (":ACQ:STOP", ":MEAS:STOP", "*RST"):
            if head == "*RST":
                self._cfg, self._trigger = {}, {}
            self.backend.stop(self._capture_id)
        else:
            raise HilError(
                f"logic driver: unsupported write '{cmd}' — see the alientek-atk-logic "
                "device profile for the supported pseudo-SCPI set"
            )

    # -- pseudo-SCPI query ------------------------------------------------
    def query(self, command: str, timeout: float | None = None) -> str:
        up = self._check(command).upper()
        if up == "*IDN?":
            return self.idn()
        if up == ":ACQ:STAT?":
            return str(self.backend.status(self._capture_id).get("state", "idle"))
        if up.startswith(":FETC"):
            art = self._fetch_current()
            return art.path
        m = re.fullmatch(r":DEC\s+(.+)\?", up)
        if m:
            art = self.decode(m.group(1).strip())
            return art.path
        if up == "*OPC?":
            return "1" if str(self.backend.status(self._capture_id).get("state", "idle")) in ("done", "idle") else "0"
        if up == "SYST:ERR?":
            return '0,"No error"'
        raise HilError(f"logic driver: unsupported query '{command}'")

    # -- rich capture API (used by the bench logic/decode steps) ----------
    def _merged(self, **overrides: Any) -> dict:
        cfg = dict(self._cfg)
        if self._trigger:
            cfg.setdefault("trigger", dict(self._trigger))
        for k, v in overrides.items():
            if v is not None:
                cfg[k] = v
        cfg.setdefault("mode", "block")
        return cfg

    def configure(self, **kwargs: Any) -> dict:
        """Push a capture configuration to the backend (channels/rate/depth/...)."""
        for k, v in kwargs.items():
            if v is not None:
                self._cfg[k] = v
        return self.backend.configure(self._merged())

    def run_capture(self, *, channels: Any = None, sample_rate_hz: float | None = None,
                    depth_samples: int | None = None, duration_s: float | None = None,
                    trigger: dict | None = None, fmt: str = "srzip", out: str | None = None,
                    mode: str = "block", timeout: float | None = None) -> CaptureArtifact:
        """Configure -> arm -> wait -> fetch, returning a file-backed artifact."""
        cfg = self._merged(channels=channels, sample_rate_hz=sample_rate_hz,
                           depth_samples=depth_samples, duration_s=duration_s,
                           trigger=trigger, mode=mode)
        self.backend.configure(cfg)
        self._capture_id = self.backend.arm()
        self.backend.wait(self._capture_id, timeout if timeout is not None else self.timeout)
        dest = "path" if out else "spool"
        res = self.backend.fetch(self._capture_id, fmt=fmt, dest=dest, path=out)
        art = self._artifact_from(res, kind="logic", fmt=fmt)
        self._last_artifact = art
        self._finalize(art, out)
        return art

    def decode(self, decoders: Any, *, artifact: CaptureArtifact | str | None = None,
               fmt: str = "csv", out: str | None = None) -> CaptureArtifact:
        """Run protocol decode over an artifact, returning a decoded artifact."""
        src = artifact if artifact is not None else self._last_artifact
        if src is None:
            raise HilError(
                "logic driver: nothing to decode — run a capture first "
                "(':INIT:ACQ' then ':FETCh?', or the 'logic' bench step)"
            )
        src_path = src.path if isinstance(src, CaptureArtifact) else str(src)
        dlist = _norm_decoders(decoders)
        if not dlist:
            raise HilError("logic driver: decode needs at least one decoder name")
        res = self.backend.decode(src_path, dlist, fmt=fmt, out=out)
        art = self._artifact_from(res, kind="decoded", fmt=fmt)
        if not art.parent and isinstance(src, CaptureArtifact):
            art.parent = src.id
        if not art.decoders:
            art.decoders = [d.get("name", "") for d in dlist]
        self._finalize(art, None)
        return art

    # -- artifact plumbing ------------------------------------------------
    def _fetch_current(self) -> CaptureArtifact:
        self.backend.wait(self._capture_id, self.timeout)
        res = self.backend.fetch(self._capture_id, fmt=self._fetch_fmt)
        art = self._artifact_from(res, kind="logic", fmt=self._fetch_fmt)
        self._last_artifact = art
        self._finalize(art, None)
        return art

    def _artifact_from(self, res: dict, *, kind: str, fmt: str) -> CaptureArtifact:
        d = dict(res or {})
        d.setdefault("kind", kind)
        d.setdefault("format", fmt)
        if not d.get("id"):
            d["id"] = new_id(kind)
        return CaptureArtifact.from_dict(d)

    def _finalize(self, art: CaptureArtifact, out: str | None) -> None:
        p = Path(art.path) if art.path else None
        local = bool(p and p.exists())
        if local:
            self.spool.record(art)      # writes the metadata sidecar + size
        if out:
            target = Path(out)
            if local and p is not None and p.resolve() != target.resolve():
                art.path = str(self.spool.retain(art.path, out))
            art.meta["out"] = str(target)

    # -- bounded compatibility capture (never loads bulk into memory) -----
    def capture(self, channel: str, seconds: float, interval: float | None = None,
                units: str = "") -> list[Sample]:
        """Small CSV capture downsampled to <= ``_CAPTURE_ROW_CAP`` points.

        The ``logic`` bench step is the real, file-backed path; this keeps the
        generic ``capture`` step working (and memory-safe) for a logic channel.
        """
        rate = float(self._cfg.get("sample_rate_hz") or _DEFAULT_RATE)
        art = self.run_capture(duration_s=float(seconds), depth_samples=_CAPTURE_ROW_CAP, fmt="csv")
        return self._samples_from_csv(art, channel, rate, units)

    def _samples_from_csv(self, art: CaptureArtifact, channel: str, rate: float,
                          units: str) -> list[Sample]:
        p = Path(art.path)
        if not p.exists():
            return []
        want = _channel_column(channel)
        out: list[Sample] = []
        t0 = time.time()
        with p.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            cols = reader.fieldnames or []
            col = want if want in cols else next((c for c in cols if c.startswith("ch")), None)
            if col is None:
                return []
            for i, row in enumerate(reader):
                if i >= _CAPTURE_ROW_CAP:
                    break
                try:
                    val = float(row.get(col, 0) or 0)
                except (TypeError, ValueError):
                    continue
                ts = t0 + (i / rate if rate else 0.0)
                out.append(Sample(ts=ts, channel=channel, value=val, units=units or "logic"))
        return out

    # -- SCPI-common lifecycle overrides ---------------------------------
    def reset(self) -> None:
        self._cfg, self._trigger = {}, {}
        self.backend.stop(self._capture_id)

    def opc(self) -> bool:
        return str(self.backend.status(self._capture_id).get("state", "idle")) in ("done", "idle")

    def wait_until_done(self, timeout: float = 30.0) -> bool:
        self.backend.wait(self._capture_id, timeout)
        return self.opc()

    def drain_errors(self) -> list[str]:
        return []

    def close(self) -> None:
        try:
            self.backend.close()
        except Exception:  # pragma: no cover - close must never raise
            pass


def _channel_column(channel: str) -> str:
    """Map a channel label ('0', 'ch0', 'CH0', 'd0') to a sim CSV column."""
    s = (channel or "").strip().lower()
    m = re.fullmatch(r"(?:ch|d)?(\d+)", s)
    if m:
        return f"ch{int(m.group(1))}"
    return s


def logic_driver(addr: Address, device: Device | None, timeout: float) -> LogicInstrument:
    """Factory for ``logic://host[:port]`` (or a device with ``driver: logic``)."""
    host = addr.host
    port = addr.port or DEFAULT_LOGIC_PORT
    if not host:
        raise HilError(
            "logic:// needs a daemon host, e.g. logic://127.0.0.1:5555 "
            "(the ATK-Logic capture daemon; see docs/logic-daemon-protocol.md)"
        )
    backend = LogicDaemonBackend(host, port, timeout=timeout)
    return LogicInstrument(backend, device=device, timeout=timeout)
