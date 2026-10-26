"""Nordic Power Profiler Kit II (PPK2) driver — a non-SCPI instrument.

The PPK2 speaks a **proprietary binary protocol over USB-CDC serial** (115200
8N1), not SCPI. Rather than reimplement it, this driver wraps the open-source
``ppk2-api`` package (IRNAS, **GPLv2**) behind a small swappable
:class:`Ppk2Backend` protocol. HIL's MIT core therefore never vendors GPL code:
install the backend yourself (``pip install 'hil-harness[ppk2]'``) or inject any
compatible object — which is also how the tests drive it with a fake.

A curated **pseudo-SCPI facade** lets the PPK2 reuse the rest of HIL unchanged —
registry safety policy, bench procedures, ``--sim`` dry-runs and CLI ``--json``::

    *IDN?  :SOUR:VOLT <mV>  :SOUR:VOLT?  :OUTP ON|OFF  :CONF:MODE SOUR|AMP
    :INIT:ACQ  :MEAS:STOP  :MEAS:CURR?  :MEAS:POWer?

The PPK2's **8 digital channels** (D0..D7) are *inputs only* — a low-end logic
analyzer for correlating code activity with power, not drivable outputs::

    :DIG<n>?   :DIG:PORT?

The PPK2 streams the 8 logic lines *inside* each sample frame, so the bundled
:class:`Ppk2ApiBackend` decodes them from ``ppk2-api``'s ``get_samples()`` (which
returns ``(analog, digital)``); a digital read returns the latest captured 8-bit
word while measuring. An injected backend that implements ``read_digital``
directly is used as-is. ``capture("digital")`` streams the input word so current
spikes can be correlated with logic edges.

Native 100 kSps sampling powers :meth:`Ppk2Instrument.capture` (µA → A, or W
when the channel is power), which the telemetry layer logs durably.
"""

from __future__ import annotations

import re
import time
from typing import Any, Protocol

from ..addresses import Address
from ..errors import HilError, TransportNotFoundError
from ..instrument import BaseInstrument
from ..registry import Device
from ..telemetry import Sample

_UA_TO_A = 1e-6
_DIG_PINS = 8

_UNSUPPORTED_DIGITAL = (
    "ppk2 driver: this backend does not expose the PPK2's 8 digital input channels. "
    "The bundled ppk2-api backend lacks them (upstream marks the digital/GUI "
    "feature set 'in progress'). Inject a backend that implements read_digital "
    "via Ppk2Instrument(backend=...)."
)
_DIGITAL_INPUT_ONLY = (
    "ppk2 driver: the PPK2's 8 digital channels are inputs only (a logic-analyzer "
    "feature) and cannot be driven as outputs. Read them with ':DIG<n>?' or "
    "':DIG:PORT?'."
)
_DIGITAL_NEEDS_ACQ = (
    "ppk2 driver: no digital data buffered yet — the PPK2 carries its 8 logic "
    "lines inside the sample frames, so start acquisition first (':SOUR:VOLT <mv>' "
    "then ':INIT:ACQ') before reading ':DIG:PORT?' or ':DIG<n>?'."
)
_START_SETTLE_S = 0.05       # let the 100 kSps stream fill the buffer after start
_CALIBRATE_RETRIES = 3       # drains of a stale serial buffer before giving up


class Ppk2Backend(Protocol):
    """The minimal, swappable surface a PPK2 backend must provide."""

    def connect(self) -> None: ...
    def use_source_meter(self) -> None: ...
    def use_amp_meter(self) -> None: ...
    def set_source_voltage(self, mv: int) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read_samples(self) -> list[float]: ...   # returns µA values
    def close(self) -> None: ...
    def info(self) -> str: ...
    # -- optional: 8 digital input channels (D0..D7) -----------------------
    def read_digital(self) -> int: ...           # returns the 8-bit input word


def _maybe_call(api: Any, *names: str, default: Any = None) -> Any:
    """Call the first present attribute among ``names`` (method or property)."""
    for n in names:
        if hasattr(api, n):
            attr = getattr(api, n)
            return attr() if callable(attr) else attr
    return default


def _first_callable(api: Any, *names: str) -> Any:
    """Return the first present callable among ``names`` (without calling it)."""
    for n in names:
        attr = getattr(api, n, None)
        if callable(attr):
            return attr
    return None


class Ppk2ApiBackend:
    """Adapter over the optional ``ppk2-api`` package (lazy import).

    Method names differ across ``ppk2-api`` versions, so each call tries the
    known aliases and degrades gracefully. ``info()`` reports the port.
    """

    def __init__(self, port: str) -> None:
        self.port = port
        self._api: Any = None
        self._last_digital: int | None = None

    def _load(self) -> Any:
        if self._api is None:
            try:
                from ppk2_api.ppk2_api import PPK2_API  # type: ignore
            except Exception:  # pragma: no cover - depends on user install
                try:
                    from ppk2_api import ppk2_api as _m  # type: ignore
                    PPK2_API = _m.PPK2_Api  # type: ignore[attr-defined]
                except Exception as exc:  # pragma: no cover
                    raise TransportNotFoundError(
                        "ppk2", "ppk2", "ppk2-api"
                    ) from exc
            self._api = PPK2_API(self.port)
            self._calibrate()
        return self._api

    def _calibrate(self) -> None:
        """Load the device's calibration modifiers, tolerating a stale buffer.

        A prior session can leave the PPK2 streaming sample bytes; ``ppk2-api``
        reads metadata as UTF-8 until ``END`` and raises ``UnicodeDecodeError``
        on that binary. Halt sampling and drain first, then retry a few times.
        Falls back to ppk2-api's built-in default modifiers rather than failing
        the connection.
        """
        api = self._api
        if not hasattr(api, "get_modifiers"):
            return
        try:
            if hasattr(api, "stop_measuring"):
                api.stop_measuring()
        except Exception:
            pass
        time.sleep(0.05)
        self._drain()
        for _ in range(_CALIBRATE_RETRIES):
            try:
                if api.get_modifiers():
                    return
            except UnicodeDecodeError:
                pass
            except Exception:
                return
            self._drain()
            time.sleep(0.12)

    def _drain(self) -> None:
        ser = getattr(self._api, "ser", None)
        for meth in ("reset_input_buffer", "flushInput"):
            fn = getattr(ser, meth, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                return

    def connect(self) -> None:
        self._load()

    def use_source_meter(self) -> None:
        _maybe_call(self._load(), "use_source_meter", "select_source_meter_mode")

    def use_amp_meter(self) -> None:
        _maybe_call(self._load(), "use_ampere_meter", "use_amp_meter", "select_amp_meter_mode")

    def set_source_voltage(self, mv: int) -> None:
        self._load().set_source_voltage(int(mv))

    def start(self) -> None:
        self._load().start_measuring()
        time.sleep(_START_SETTLE_S)   # let the 100 kSps stream fill the buffer

    def stop(self) -> None:
        _maybe_call(self._load(), "stop_measuring")

    def _read_frames(self) -> tuple[list[float], list[int]]:
        """Read one sampling period -> (analog µA values, 8-bit digital words).

        ``ppk2-api`` >= 0.9 returns ``(samples, raw_digital_output)``; older
        builds returned a flat list of analog floats. Both shapes are handled so
        the driver works across versions.
        """
        api = self._load()
        data = api.get_data()
        if not data:
            return [], []
        res = api.get_samples(data)
        if isinstance(res, tuple) and len(res) == 2:
            analog, digital = res
        else:                       # legacy: a flat list of analog floats
            analog, digital = res, []
        analog = [float(x) for x in (analog or [])]
        digital = [int(b) & 0xFF for b in (digital or [])]
        if digital:
            self._last_digital = digital[-1]
        return analog, digital

    def read_samples(self) -> list[float]:
        return self._read_frames()[0]

    def set_output(self, on: bool) -> None:
        """Enable/disable DUT power (source mode) via ppk2-api's toggle_DUT_power."""
        fn = _first_callable(self._load(), "toggle_DUT_power")
        if fn is not None:
            fn("ON" if on else "OFF")

    def close(self) -> None:
        try:
            self.stop()
        except Exception:  # pragma: no cover - close must never raise
            pass

    def info(self) -> str:
        return f"ppk2-api@{self.port}"

    # -- 8 digital input channels (D0..D7) --------------------------------
    # The PPK2 carries the logic lines inside each sample frame, so a read
    # returns the latest captured 8-bit word (while measuring). A dedicated
    # read_digital-style method on another backend is used if present.

    def read_digital(self) -> int:
        api = self._load()
        if callable(getattr(api, "get_data", None)) and callable(getattr(api, "get_samples", None)):
            _analog, digital = self._read_frames()
            if digital:
                return digital[-1]
            if self._last_digital is not None:
                return self._last_digital
            raise HilError(_DIGITAL_NEEDS_ACQ)
        fn = _first_callable(
            api, "read_digital", "get_digital_channels",
            "read_digital_port", "get_gpio",
        )
        if fn is None:
            raise HilError(_UNSUPPORTED_DIGITAL)
        return int(fn()) & 0xFF


class Ppk2Instrument(BaseInstrument):
    """A PPK2 presented through HIL's :class:`~hil.instrument.Instrument` verbs."""

    def __init__(
        self,
        port: str,
        device: Device | None = None,
        timeout: float = 5.0,
        backend: Ppk2Backend | None = None,
    ) -> None:
        super().__init__(device)
        self.port = port
        self.timeout = timeout
        self.backend: Ppk2Backend = backend or Ppk2ApiBackend(port)
        self.backend.connect()
        self._vset_mv = 0
        self._measuring = False

    # -- identity -----------------------------------------------------------
    def idn(self) -> str:
        return f"Nordic,PPK2,{self.port},{self.backend.info()}"

    # -- pseudo-SCPI write --------------------------------------------------
    def write(self, command: str) -> None:
        cmd = self._check(command)
        head, _, arg = cmd.partition(" ")
        head, arg = head.strip().upper(), arg.strip().upper()
        if head == ":SOUR:VOLT":
            self._vset_mv = int(float(arg))
            self.backend.set_source_voltage(self._vset_mv)
        elif head == ":OUTP":
            if arg.startswith("ON") or arg == "1":
                self.backend.use_source_meter()
                self._set_dut_power(True)
            else:
                self._set_dut_power(False)
                self.backend.stop()
                self._measuring = False
        elif head == ":CONF:MODE":
            if arg.startswith("AMP"):
                self.backend.use_amp_meter()
            else:
                self.backend.use_source_meter()
        elif head == ":INIT:ACQ":
            self.backend.start()
            self._measuring = True
        elif head in (":MEAS:STOP", "*RST"):
            self.backend.stop()
            self._measuring = False
        elif head.startswith(":DIG"):
            raise HilError(_DIGITAL_INPUT_ONLY)
        else:
            raise HilError(
                f"ppk2 driver: unsupported write '{cmd}' — see the nordic-ppk2 "
                "device profile for the supported pseudo-SCPI set"
            )

    # -- digital inputs (8 channels, D0..D7; read-only) --------------------
    def _read_port(self) -> int:
        return int(self.backend.read_digital()) & 0xFF

    # -- pseudo-SCPI query --------------------------------------------------
    def query(self, command: str, timeout: float | None = None) -> str:
        up = self._check(command).upper()
        if up == "*IDN?":
            return self.idn()
        if up == ":SOUR:VOLT?":
            return str(self._vset_mv)
        if up == ":MEAS:CURR?":
            return f"{self._mean_current():.9g}"
        if up in (":MEAS:POW?", ":MEAS:POWER?"):
            return f"{self._mean_power():.9g}"
        if up == ":DIG:PORT?":
            return str(self._read_port())
        m = re.fullmatch(r":DIG(\d)\?", up)
        if m:
            pin = int(m.group(1))
            if not 0 <= pin < _DIG_PINS:
                raise HilError(f"ppk2 driver: digital pin must be 0..{_DIG_PINS - 1}, got {pin}")
            return str((self._read_port() >> pin) & 1)
        if up == "*OPC?":
            return "1"
        if up == "SYST:ERR?":
            return '0,"No error"'
        raise HilError(f"ppk2 driver: unsupported query '{command}'")

    # -- native sampling ----------------------------------------------------
    def _set_dut_power(self, on: bool) -> None:
        fn = getattr(self.backend, "set_output", None)
        if callable(fn):
            fn(on)

    def _ensure_measuring(self) -> None:
        if not self._measuring:
            if not self._vset_mv:
                raise HilError(
                    "ppk2 driver: set a voltage before measuring — send "
                    "':SOUR:VOLT <mv>' (e.g. ':SOUR:VOLT 3300') first; the PPK2 "
                    "needs an input/output voltage reference to sample (ppk2-api "
                    "raises 'Input voltage not set!' otherwise)."
                )
            self.backend.start()
            self._measuring = True

    def _mean_current(self) -> float:
        self._ensure_measuring()
        samples = self.backend.read_samples()
        return (sum(samples) / len(samples)) * _UA_TO_A if samples else 0.0

    def _mean_power(self) -> float:
        return self._mean_current() * (self._vset_mv / 1000.0)

    # -- SCPI-common lifecycle overrides -----------------------------------
    def reset(self) -> None:
        self.backend.stop()
        self._measuring = False

    def opc(self) -> bool:
        return True

    def wait_until_done(self, timeout: float = 30.0) -> bool:
        return True

    def drain_errors(self) -> list[str]:
        return []

    def capture(
        self,
        channel: str,
        seconds: float,
        interval: float | None = None,
        units: str = "",
    ) -> list[Sample]:
        """Native high-rate capture.

        Channels: ``current``/``:MEAS:CURR?`` → amperes; ``power``/``:MEAS:POW?``
        → watts (V·I); ``digital``/``:DIG:PORT?`` → the 8-bit input word polled at
        ``interval`` (for correlating current with logic edges).
        """
        ch = (channel or "current").lower()
        as_digital = "dig" in ch
        as_power = "pow" in ch
        if not as_digital:
            self._ensure_measuring()
        vout = self._vset_mv / 1000.0
        out: list[Sample] = []
        end = time.monotonic() + float(seconds)
        step = float(interval) if interval else 0.05
        while time.monotonic() < end:
            ts = time.time()
            if as_digital:
                out.append(Sample(ts=ts, channel=channel, value=float(self._read_port()), units=units or "input"))
            else:
                for ua in self.backend.read_samples():
                    amps = ua * _UA_TO_A
                    if as_power:
                        out.append(Sample(ts=ts, channel=channel, value=amps * vout, units=units or "W"))
                    else:
                        out.append(Sample(ts=ts, channel=channel, value=amps, units=units or "A"))
            time.sleep(step)
        return out

    def close(self) -> None:
        # De-energize first: closing must never leave VOUT sourcing. A failed
        # bench procedure skips its ':OUTP OFF' cleanup, so close() is the last
        # line of defence (a no-op for backends without set_output).
        try:
            self._set_dut_power(False)
        except Exception:  # pragma: no cover
            pass
        try:
            self.backend.stop()
        except Exception:  # pragma: no cover
            pass
        try:
            self.backend.close()
        except Exception:  # pragma: no cover
            pass


def ppk2_driver(addr: Address, device: Device | None, timeout: float) -> Ppk2Instrument:
    """Factory for ``ppk2://<serial-port>`` (or a device with ``driver: ppk2``)."""
    port = addr.path or addr.host or ""
    if not port:
        raise HilError(
            "ppk2:// needs a serial port, e.g. ppk2:///dev/cu.usbmodem01 "
            "(macOS), ppk2:///dev/ttyACM0 (Linux) or ppk2://COM3 (Windows)"
        )
    return Ppk2Instrument(port, device=device, timeout=timeout)
