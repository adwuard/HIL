"""The instrument abstraction — one interface over any hardware.

HIL originally spoke only SCPI (:class:`~hil.scpi.ScpiInstrument`). Real benches
mix in devices that are *not* SCPI: power profilers (Nordic PPK2), OCD debuggers
(OpenOCD/pyOCD), logic analysers (sigrok/Saleae), or a custom USB-CDC supply. This
module defines the verb surface the bench, CLI and safety layer actually rely on,
so any of those can plug in as a *driver* without touching the harness.

Two pieces:

* :class:`Instrument` — a structural :class:`~typing.Protocol` describing the
  verbs (``idn/write/query/execute/safe_execute/reset/opc/wait_until_done/
  drain_errors/close/capture``).
* :class:`BaseInstrument` — a concrete base that implements the SCPI-common
  verbs in terms of ``write``/``query``, enforces the registry
  :class:`~hil.registry.SafetyPolicy`, and provides a default poll-based
  :meth:`capture`. Drivers subclass it and override only what's device-specific
  (a PPK2 overrides ``write``/``query``/``capture``; an SCPI device overrides
  nothing but the transport I/O).

Adding a new non-SCPI device = subclass :class:`BaseInstrument`, register a
factory in :mod:`hil.drivers`, and ship a registry YAML with ``driver: <name>``.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from .errors import HilError
from .registry import Device
from .telemetry import Sample


@runtime_checkable
class Instrument(Protocol):
    """The verbs the bench/CLI depend on; satisfied by every HIL driver."""

    device: Device | None

    def idn(self) -> str: ...
    def identify(self) -> dict[str, str]: ...
    def write(self, command: str) -> None: ...
    def query(self, command: str, timeout: float | None = None) -> str: ...
    def execute(self, command: str) -> str: ...
    def safe_execute(self, command: str, timeout: float | None = None) -> str: ...
    def reset(self) -> None: ...
    def opc(self) -> bool: ...
    def wait_until_done(self, timeout: float = 30.0) -> bool: ...
    def drain_errors(self) -> list[str]: ...
    def close(self) -> None: ...
    def capture(
        self, channel: str, seconds: float, interval: float | None = None, units: str = ""
    ) -> list[Sample]: ...


class BaseInstrument(ABC):
    """Shared behaviour for every driver.

    Subclasses must implement :meth:`idn`, :meth:`write` and :meth:`query`; the
    rest default to their SCPI-common meaning and can be overridden.
    """

    def __init__(self, device: Device | None = None) -> None:
        self.device = device

    # -- safety -------------------------------------------------------------
    def _check(self, command: str) -> str:
        """Run the registry safety policy (if a device profile is attached)."""
        if self.device is not None:
            self.device.check_command(command)
        return command.strip()

    # -- identity -----------------------------------------------------------
    @abstractmethod
    def idn(self) -> str: ...

    def identify(self) -> dict[str, str]:
        parts = [p.strip() for p in self.idn().split(",")]
        while len(parts) < 4:
            parts.append("")
        return {
            "manufacturer": parts[0],
            "model": parts[1],
            "serial": parts[2],
            "firmware": parts[3],
        }

    # -- I/O ----------------------------------------------------------------
    @abstractmethod
    def write(self, command: str) -> None: ...

    @abstractmethod
    def query(self, command: str, timeout: float | None = None) -> str: ...

    def execute(self, command: str) -> str:
        command = command.strip()
        if command.endswith("?"):
            return self.query(command)
        self.write(command)
        return ""

    def safe_execute(self, command: str, timeout: float | None = None) -> str:
        """Single-transaction execute: query with a timeout, or write."""
        command = command.strip()
        if command.endswith("?"):
            return self.query(command, timeout)
        self.write(command)
        return ""

    # -- SCPI-common lifecycle (drivers may override) -----------------------
    def reset(self) -> None:
        self.write("*RST")

    def opc(self) -> bool:
        return self.query("*OPC?").strip() == "1"

    def wait_until_done(self, timeout: float = 30.0) -> bool:
        return self.opc()

    def drain_errors(self) -> list[str]:
        """Poll ``SYST:ERR?`` until the queue reports empty (bounded)."""
        out: list[str] = []
        for _ in range(32):
            try:
                msg = self.query("SYST:ERR?")
            except HilError:
                break
            if msg is None:
                break
            msg = msg.strip()
            if not msg or msg.startswith(("0,", '"0,', "0 \"", '+0')):
                break
            out.append(msg)
        return out

    def close(self) -> None:  # pragma: no cover - default no-op
        pass

    # -- telemetry ----------------------------------------------------------
    def capture(
        self,
        channel: str,
        seconds: float,
        interval: float | None = None,
        units: str = "",
    ) -> list[Sample]:
        """Default capture: poll ``query(channel)`` every ``interval`` seconds.

        Streaming devices (e.g. a power profiler) override this with native
        high-rate sampling; query-based devices get durable logging for free.
        """
        interval = float(interval) if interval else 0.1
        end = time.monotonic() + float(seconds)
        samples: list[Sample] = []
        while time.monotonic() < end:
            ts = time.time()
            try:
                resp = self.query(channel)
                val: float | None = float(resp.strip())
            except (HilError, TypeError, ValueError):
                val = None
            if val is not None:
                samples.append(Sample(ts=ts, channel=channel, value=val, units=units))
            time.sleep(interval)
        return samples

    # -- convenience --------------------------------------------------------
    def __enter__(self) -> "BaseInstrument":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
