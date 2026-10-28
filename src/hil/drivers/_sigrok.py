"""Sigrok (``sigrok-cli``) logic-analyzer backend — subprocess isolation for GPL libsigrok.

Some analyzers are driven only by **libsigrok** (GPL-3.0). The Sipeed **SLogic
Combo 8** — an 8-channel USB2.0 analyzer (``VID:PID 359f:0300``, "SIPEED USB TO
LA", <=80 MS/s) — is one of them: Sipeed ships its support as the
``sipeed-slogic-analyzer`` driver on their ``libsigrok`` ``slogic-dev`` fork, with
no documented standalone USB protocol. HIL's MIT core therefore never links
libsigrok; it *shells out* to ``sigrok-cli`` — exactly the isolation the debug
driver uses for GPL OpenOCD. Copyleft code stays in a child process.

Because ``sigrok-cli`` acquires-and-saves in one blocking call, this backend
mirrors :class:`~hil.drivers._atklogic.AtkLogicBackend`: :meth:`arm` runs the
acquisition into a spool ``.srzip`` and caches it, ``status``/``wait`` then report
``done``, and :meth:`fetch` returns that artifact (converting to ``csv`` via
``sigrok-cli -i … -O csv`` on request). :meth:`decode` runs sigrok protocol
decoders (``-P i2c:scl=0:sda=1``) over the capture and spools the annotations.

The argv builders are **pure** (no PATH lookup or I/O) so they are unit-testable
with no ``sigrok-cli`` installed, and ``runner`` is an injection seam so tests can
drive the whole capture/decode flow with a fake subprocess.

Addressing: ``logic://sigrok`` (aliases ``slogic`` / ``sipeed``), optionally
``?driver=<libsigrok-driver>&conn=<vid.pid|bus.addr>&channels=<n>&bin=<path>``.
A stock distro ``sigrok-cli`` lacks the Sipeed driver — ``hil setup
sipeed-slogic-combo8`` prints the fork-build plan.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..artifacts import ArtifactSpool, new_id
from ..errors import HilError

_SIGROK_BINS = ("sigrok-cli",)

# Sipeed SLogic Combo 8 defaults (overridable via logic://sigrok?driver=..&conn=..).
DEFAULT_SIGROK_DRIVER = "sipeed-slogic-analyzer"
SLOGIC_VENDOR = "Sipeed"
SLOGIC_MODEL = "SLogic Combo 8"
SLOGIC_USB_ID = "359F:0300"
SLOGIC_CHANNELS = 8
SLOGIC_MAX_RATE_HZ = 80_000_000
# The SLogic8 U2 arms acquisitions in 32768-sample blocks. Requesting fewer than
# a block — or exactly one block, which proved flaky on hardware — stalls the USB
# endpoint, so sample-count captures floor at two blocks. ``--time`` mode is exempt.
SLOGIC_MIN_SAMPLES = 65536

_DEFAULT_RATE_HZ = 1_000_000.0
_DEFAULT_DEPTH = 100_000
# A representative set of sigrok protocol decoders (informational; hello() only).
_COMMON_DECODERS = ["i2c", "spi", "uart", "1wire", "can", "modbus", "sdio", "jtag"]

# libsigrok pseudo-drivers that are not real hardware; a global ``--scan``
# always lists ``demo``, so it is filtered out of discovery enumeration.
_SIM_DRIVERS = frozenset({"demo"})

# `<driver>[:conn=<vid.pid|bus.addr>] - <vendor> <model> [S/N: <ser>] with N channels: <names…>`
# sigrok names channels per the hardware silkscreen (SLogic: D0..D7; fx2lafw: 0..7),
# so the trailing name list is captured to resolve HIL indices -> sigrok names.
_SCAN_RE = re.compile(
    r"^(?P<driver>[a-z0-9][a-z0-9_-]*)"
    r"(?::conn=(?P<conn>\S+))?"
    r"\s+-\s+(?P<name>.+?)\s+with\s+(?P<nch>\d+)\s+channels?"
    r"(?::\s*(?P<chans>[A-Za-z0-9_,.\s-]+))?",
    re.IGNORECASE,
)
_SN_RE = re.compile(r"\[S/N:\s*(?P<sn>[^\]]+)\]")


# -- helpers (pure) ---------------------------------------------------------
def _tail(res: dict | None, n: int = 400) -> str:
    """A short, single-line summary of a subprocess result for error messages."""
    if not res:
        return ""
    out = (res.get("output") or res.get("stderr") or "").strip()
    if res.get("timeout"):
        out = (out + " [timed out]").strip()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:n]


def _driver_spec(driver: str, conn: str) -> str:
    """``sipeed-slogic-analyzer`` or ``…:conn=359f.0300`` when a conn is given."""
    d = (driver or "").strip()
    c = (conn or "").strip()
    return f"{d}:conn={c}" if d and c else d


def _rate_arg(sample_rate_hz: Any) -> str:
    """sigrok ``samplerate=`` accepts a plain Hz count — pass an integer string."""
    return str(int(float(sample_rate_hz or _DEFAULT_RATE_HZ)))


def _time_arg(duration_s: Any) -> str:
    """sigrok ``--time`` takes ms, or a value suffixed with ``s`` for seconds."""
    return f"{float(duration_s):g}s"


def _channels_arg(channels: Any, names: list[str] | None = None) -> str:
    """Map a HIL channel spec to a sigrok ``--channels`` NAME list ("" = all/omit).

    sigrok selects channels by their hardware names, not indices, so integer
    indices are resolved against the device's real ``names`` (falling back to
    ``D<i>``); a name/range string is passed through. Selecting every channel is
    sigrok's default, so it collapses to "" and the flag is omitted.
    """
    if channels is None or isinstance(channels, bool):
        return ""
    names = list(names or [])
    if isinstance(channels, (list, tuple)):
        sel: list[Any] = list(channels)
    elif isinstance(channels, int):
        sel = list(range(channels))
    else:
        s = str(channels).strip()
        return s if s else ""                       # explicit name/range string
    toks: list[str] = []
    for c in sel:
        if isinstance(c, int) and not isinstance(c, bool):
            toks.append(names[c] if 0 <= c < len(names) else f"D{c}")
        else:
            toks.append(str(c))
    if names and len(toks) >= len(names):
        return ""                                   # every channel -> omit
    return ",".join(toks)


def _trigger_arg(trigger: Any, names: list[str] | None = None) -> str:
    """Map a HIL trigger to sigrok ``--triggers <chan>=<0|1|r|f>`` ("" = none).

    ``<chan>`` is resolved to the device's channel name when ``names`` is known
    (sigrok accepts a name or a number); with no names it falls back to the index.
    """
    names = list(names or [])

    def _ch(idx: int) -> str:
        return names[idx] if 0 <= idx < len(names) else str(idx)

    if isinstance(trigger, str):
        t = trigger.strip().lower()
        if t in ("r", "rising", "rise"):
            return f"{_ch(0)}=r"
        if t in ("f", "falling", "fall"):
            return f"{_ch(0)}=f"
        return ""
    if not isinstance(trigger, dict) or not trigger:
        return ""
    raw = trigger.get("channel", 0)
    if isinstance(raw, str) and not raw.strip().lstrip("-").isdigit():
        cname = raw.strip()                         # already a sigrok channel name
    else:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            idx = 0
        cname = _ch(idx)
    edge = str(trigger.get("edge", "") or "").lower()
    if edge in ("rising", "rise", "r"):
        return f"{cname}=r"
    if edge in ("falling", "fall", "f"):
        return f"{cname}=f"
    level = trigger.get("level")
    if level in (1, "1", True):
        return f"{cname}=1"
    if level in (0, "0", False):
        return f"{cname}=0"
    return ""


def _decoder_spec(d: Any, names: list[str] | None = None) -> str:
    """One ``-P`` argument: ``i2c`` or ``i2c:scl=D6:sda=D7[:baudrate=100000]``.

    Decoder channel-map values given as integer indices (or numeric strings) are
    resolved to the device's sigrok channel names — the SLogic names them D0..D7
    and rejects bare indices (``cli: No channel with name "6" found``). A real
    channel-name string is passed through unchanged.
    """
    if isinstance(d, str):
        return d.strip()
    if not isinstance(d, dict):
        return str(d)
    names = list(names or [])

    def _cname(ch: Any) -> Any:
        if isinstance(ch, bool):
            return ch
        if isinstance(ch, int):
            return names[ch] if 0 <= ch < len(names) else f"D{ch}"
        if isinstance(ch, str) and ch.strip().lstrip("-").isdigit():
            i = int(ch.strip())
            return names[i] if 0 <= i < len(names) else f"D{i}"
        return ch

    name = str(d.get("name") or d.get("decoder") or "").strip()
    parts = [name] if name else []
    for k, v in dict(d.get("options", {}) or {}).items():
        parts.append(f"{k}={v}")
    cmap = d.get("channels") or d.get("map") or {}
    for sig, ch in dict(cmap).items():
        parts.append(f"{sig}={_cname(ch)}")
    return ":".join(parts)


def parse_scan(output: str) -> list[dict]:
    """Best-effort parse of ``sigrok-cli --scan`` output into device records."""
    found: list[dict] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("the following", "sr:", "libsigrok")):
            continue
        m = _SCAN_RE.match(line)
        if not m:
            continue
        name = m.group("name") or ""
        sn = _SN_RE.search(name)
        serial = sn.group("sn").strip() if sn else ""
        model = _SN_RE.sub("", name).strip()
        chans = (m.group("chans") or "").strip()
        channel_names = [c for c in re.split(r"[,\s]+", chans) if c]
        found.append({
            "driver": m.group("driver"),
            "conn": m.group("conn") or "",
            "model": model,
            "serial": serial,
            "channels": int(m.group("nch")),
            "channel_names": channel_names,
        })
    return found


# -- the backend ------------------------------------------------------------
class SigrokBackend:
    """A :class:`~hil.drivers.logic.LogicBackend` that shells out to ``sigrok-cli``.

    ``runner`` (``(argv, timeout) -> result-dict``) is an injection seam for
    hardware-free tests; when omitted, :meth:`_run` uses :mod:`subprocess`. The
    result dict mirrors the debug driver's: ``{rc, stdout, stderr, output, timeout}``.
    """

    def __init__(self, driver: str = DEFAULT_SIGROK_DRIVER, *, conn: str = "",
                 serial: str = "", channels: int = SLOGIC_CHANNELS, sigrok_bin: str = "",
                 spool: ArtifactSpool | None = None, timeout: float = 30.0,
                 runner: Callable[[list[str], float], dict] | None = None) -> None:
        self.driver = (driver or DEFAULT_SIGROK_DRIVER).strip()
        self.conn = (conn or "").strip()
        self.serial = (serial or "").strip()
        self.channels = int(channels or SLOGIC_CHANNELS)
        # sigrok selects channels by hardware name; default to the D<i> convention
        # (correct for the SLogic) and refine from a live scan in hello().
        self.channel_names: list[str] = [f"D{i}" for i in range(self.channels)]
        # Enforce the device's acquisition-block floor for sample-count captures.
        self.min_samples = SLOGIC_MIN_SAMPLES if "slogic" in self.driver.lower() else 0
        self.spool = spool or ArtifactSpool()
        self.timeout = timeout
        self._runner = runner
        self._bin = sigrok_bin or _SIGROK_BINS[0]
        self._cfg: dict[str, Any] = {}
        self._caps: dict[str, dict] = {}
        self._cid = ""
        self._state = "idle"

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        if self._runner is not None:
            return                                  # injected: no PATH requirement
        for cand in _SIGROK_BINS:
            found = shutil.which(cand)
            if found:
                self._bin = found
                break
        else:
            raise self._missing_tool_error()

    def close(self) -> None:
        self._state = "idle"

    def info(self) -> str:
        return f"sigrok:{self.driver}@{self._bin}" + (f":{self.conn}" if self.conn else "")

    def _missing_tool_error(self) -> HilError:
        return HilError(
            "logic driver: sigrok-cli is not installed or not on PATH. The Sipeed "
            "SLogic Combo 8 needs a sigrok-cli built against Sipeed's libsigrok fork "
            f"(driver '{DEFAULT_SIGROK_DRIVER}'); a stock sigrok-cli lacks it. Install "
            "with `hil setup sipeed-slogic-combo8` (or run `hil doctor`)."
        )

    # -- pure argv builders (unit-testable; no I/O) -----------------------
    def scan_argv(self) -> list[str]:
        if self.driver:
            return [self._bin, "--driver", self.driver, "--scan"]
        return [self._bin, "--scan"]

    def capture_argv(self, out_path: Any, *, sample_rate_hz: Any = None,
                     channels: Any = None, samples: int | None = None,
                     duration_s: Any = None, trigger: Any = None) -> list[str]:
        argv = [self._bin, "--driver", _driver_spec(self.driver, self.conn)]
        if sample_rate_hz:
            argv += ["--config", f"samplerate={_rate_arg(sample_rate_hz)}"]
        ch = _channels_arg(channels, self.channel_names)
        if ch:
            argv += ["--channels", ch]
        if samples:
            argv += ["--samples", str(int(samples))]
        elif duration_s:
            argv += ["--time", _time_arg(duration_s)]
        trig = _trigger_arg(trigger, self.channel_names)
        if trig:
            argv += ["--triggers", trig]
        argv += ["--output-format", "srzip", "-o", str(out_path)]
        return argv

    def convert_argv(self, src: Any, out: Any, fmt: str = "csv") -> list[str]:
        return [self._bin, "--input-file", str(src), "--output-format", fmt or "csv",
                "--output-file", str(out)]

    def decode_argv(self, src: Any, decoders: list[Any]) -> list[str]:
        argv = [self._bin, "--input-file", str(src)]
        for d in decoders or []:
            spec = _decoder_spec(d, self.channel_names)
            if spec:
                argv += ["--protocol-decoders", spec]
        return argv

    # -- subprocess runner ------------------------------------------------
    def _run(self, argv: list[str], *, timeout: float | None = None) -> dict:
        if self._runner is not None:
            return self._runner(argv, timeout if timeout is not None else self.timeout)
        try:
            cp = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout if timeout is not None else self.timeout)
        except FileNotFoundError:
            raise self._missing_tool_error()
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return {"rc": None, "stdout": "", "stderr": out, "output": out, "timeout": True}
        err = cp.stderr or ""
        return {"rc": cp.returncode, "stdout": cp.stdout or "", "stderr": err,
                "output": (cp.stdout or "") + (("\n" + err) if err else ""), "timeout": False}

    # -- identity ---------------------------------------------------------
    def scan(self) -> list[dict]:
        """Run ``sigrok-cli --scan`` and return parsed device records (best-effort)."""
        res = self._run(self.scan_argv(), timeout=max(self.timeout, 10.0))
        if res.get("rc") not in (0, None):
            return []
        return parse_scan(res.get("output", ""))

    def hello(self) -> dict:
        dev = {"vendor": SLOGIC_VENDOR, "model": SLOGIC_MODEL,
               "serial": self.serial, "variant": "sigrok"}
        chans, usb = self.channels, SLOGIC_USB_ID
        try:                                        # enrich from a live scan; never fail
            found = self.scan()
            if found:
                d0 = found[0]
                dev = {"vendor": SLOGIC_VENDOR, "model": d0.get("model") or SLOGIC_MODEL,
                       "serial": d0.get("serial") or self.serial, "variant": "sigrok"}
                if d0.get("channels"):
                    chans = int(d0["channels"])
                if d0.get("channel_names"):
                    self.channel_names = list(d0["channel_names"])
                if d0.get("conn"):
                    usb = d0["conn"]
        except Exception:  # pragma: no cover - an offline/absent analyzer is tolerated
            pass
        return {
            "protocol": "1", "device": dev, "channels": chans,
            "max_sample_rate_hz": SLOGIC_MAX_RATE_HZ,
            "decoders": list(_COMMON_DECODERS), "spool_dir": str(self.spool.root),
            "busy": False, "usb_id": usb, "driver": self.driver,
        }

    # -- capture flow -----------------------------------------------------
    def configure(self, config: dict) -> dict:
        self._cfg = dict(config or {})
        return {"ok": True, "config": self._cfg}

    def _capture_params(self) -> dict:
        cfg = self._cfg
        rate = float(cfg.get("sample_rate_hz") or _DEFAULT_RATE_HZ)
        chans = _capture_channels(cfg.get("channels"), self.channels)
        depth = cfg.get("depth_samples")
        duration = float(cfg.get("duration_s") or 0.0)
        floor = max(1, self.min_samples)
        if depth:
            # Sample-count mode: clamp up to the device's block floor so a small
            # request can't stall the endpoint (the SLogic arms 32768-sample blocks).
            samples, duration = max(int(depth), floor), 0.0
        elif duration:
            # Duration mode streams via --time, which the device honors at any
            # length; leave samples=0 so capture_argv selects the --time path.
            samples = 0
        else:
            samples = max(_DEFAULT_DEPTH, floor)
        return {"rate": rate, "channels": chans, "samples": samples,
                "duration_s": duration, "trigger": cfg.get("trigger")}

    def arm(self) -> str:
        p = self._capture_params()
        out = self.spool.new_path("logic", "srzip")
        argv = self.capture_argv(out, sample_rate_hz=p["rate"], channels=p["channels"],
                                 samples=p["samples"], duration_s=p["duration_s"],
                                 trigger=p["trigger"])
        dur = p["duration_s"] or (p["samples"] / p["rate"] if p["rate"] else 0.0)
        res = self._run(argv, timeout=max(self.timeout, 60.0) + dur)
        cid = new_id("cap")
        ok = res.get("rc") in (0, None) and Path(out).exists()
        self._caps[cid] = {"path": out, "rate": p["rate"], "channels": p["channels"],
                           "samples": p["samples"], "duration_s": dur, "ok": ok,
                           "trigger": dict(p["trigger"] or {}) if isinstance(p["trigger"], dict) else {}}
        self._cid = cid
        self._state = "done" if ok else "error"
        if not ok:
            raise HilError(
                f"logic driver: sigrok capture failed ({self.driver}): "
                f"{_tail(res) or 'no data written'}"
            )
        return cid

    def status(self, capture_id: str = "") -> dict:
        cap = self._caps.get(capture_id or self._cid)
        return {"state": self._state, "progress": 1.0 if cap and cap.get("ok") else 0.0,
                "samples_captured": cap["samples"] if cap else 0,
                "complete": bool(cap and cap.get("ok"))}

    def wait(self, capture_id: str = "", timeout_s: float = 30.0) -> dict:
        return self.status(capture_id)          # the capture already ran in arm()

    def _cap(self, capture_id: str = "") -> dict:
        cap = self._caps.get(capture_id or self._cid)
        if cap is None:
            raise HilError("logic driver: sigrok has no capture to fetch — arm first")
        if not cap.get("ok"):
            raise HilError("logic driver: the last sigrok capture failed; nothing to fetch")
        return cap

    def fetch(self, capture_id: str = "", fmt: str = "srzip",
              dest: str = "spool", path: str | None = None) -> dict:
        cap = self._cap(capture_id)
        fmt = (fmt or "srzip").lower()
        src = Path(cap["path"])
        if fmt in ("srzip", "sr", ""):
            p = self._to_path(src, path, ".srzip") if (dest == "path" and path) else src
        elif fmt == "csv":
            p = self._dest_path(path, dest, "logic", ".csv")
            res = self._run(self.convert_argv(src, p, "csv"), timeout=max(self.timeout, 60.0))
            if res.get("rc") not in (0, None) or not Path(p).exists():
                raise HilError(f"logic driver: sigrok srzip->csv failed: {_tail(res)}")
        else:
            raise HilError(
                f"logic driver: sigrok fetch format '{fmt}' unsupported (use srzip|csv)"
            )
        return {
            "id": new_id("logic"), "kind": "logic", "path": str(p), "format": fmt or "srzip",
            "channels": cap["channels"], "sample_rate_hz": cap["rate"],
            "sample_count": cap["samples"], "duration_s": cap["duration_s"],
            "bytes": Path(p).stat().st_size if Path(p).exists() else 0,
            "trigger": cap.get("trigger", {}),
        }

    def decode(self, artifact_path: str, decoders: list[dict],
               fmt: str = "csv", out: str | None = None) -> dict:
        src = Path(artifact_path)
        if not src.exists():
            raise HilError(f"logic driver: sigrok cannot decode a missing artifact: {src}")
        dlist = list(decoders or [])
        if not dlist:
            raise HilError("logic driver: sigrok decode needs at least one decoder name")
        res = self._run(self.decode_argv(src, dlist), timeout=max(self.timeout, 120.0))
        if res.get("rc") not in (0, None):
            raise HilError(f"logic driver: sigrok decode failed: {_tail(res)}")
        text = res.get("stdout") or res.get("output") or ""
        p = self._dest_path(out, "path" if out else "spool", "decoded",
                            ".json" if (fmt or "csv").lower() == "json" else ".csv")
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(text, encoding="utf-8")
        frames = sum(1 for ln in text.splitlines() if ln.strip())
        names = [d.get("name", "") if isinstance(d, dict) else str(d) for d in dlist]
        return {
            "id": new_id("dec"), "kind": "decoded", "path": str(p),
            "format": (fmt or "csv").lower(), "frames": frames,
            "decoders": [n for n in names if n],
            "bytes": Path(p).stat().st_size if Path(p).exists() else 0,
        }

    def stop(self, capture_id: str = "") -> None:
        self._state = "idle"                    # sigrok-cli captures are one-shot

    # -- path helpers -----------------------------------------------------
    def _dest_path(self, path: str | None, dest: str, kind: str, suffix: str) -> Path:
        if dest == "path" and path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p if p.suffix else p.with_suffix(suffix)
        return self.spool.new_path(kind, "csv" if suffix == ".csv" else
                                   ("json" if suffix == ".json" else "srzip"))

    @staticmethod
    def _to_path(src: Path, path: str | None, suffix: str) -> Path:
        p = Path(str(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.suffix:
            p = p.with_suffix(suffix)
        if src.resolve() != p.resolve():
            shutil.copy2(src, p)
        return p


def _capture_channels(channels: Any, default: int) -> list[int]:
    """Normalize a configured channel spec to a concrete ``[int]`` list."""
    if isinstance(channels, bool) or channels is None:
        return list(range(default))
    if isinstance(channels, int):
        return list(range(channels))
    if isinstance(channels, (list, tuple)):
        return [int(c) for c in channels]
    s = str(channels).strip()
    if re.fullmatch(r"\d+", s):
        return list(range(int(s)))
    return [int(x) for x in re.split(r"[,\s]+", s) if x.strip()] or list(range(default))


def scan_devices(*, driver: str = "", sigrok_bin: str = "", timeout: float = 15.0,
                 runner: Callable[[list[str], float], dict] | None = None) -> list[dict]:
    """Enumerate logic analyzers via ``sigrok-cli --scan`` (for discovery).

    With no ``driver`` this is a global scan across every libsigrok driver; a
    best-effort parse means a driver that errors only drops its own line.
    """
    be = SigrokBackend(driver=driver or "", sigrok_bin=sigrok_bin, timeout=timeout,
                       runner=runner)
    argv = be.scan_argv() if driver else [be._bin, "--scan"]
    res = be._run(argv, timeout=timeout)
    if res.get("rc") not in (0, None):
        return []
    return [d for d in parse_scan(res.get("output", ""))
            if d.get("driver") not in _SIM_DRIVERS]
