"""SCPI instrument layer: the verbs every SCPI instrument understands.

Sits on any Transport and adds the standard IEEE-488.2 common commands
(*IDN?, *OPC?, *RST), the error queue (SYSTem:ERR?), and query/write with
optional per-device safety policies from the registry.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from .errors import InstrumentError, TimeoutError_
from .instrument import BaseInstrument

if TYPE_CHECKING:  # pragma: no cover
    from .registry.schema import Device
    from .transports.base import BaseTransport

MAX_QUEUE_READS = 50  # instruments can queue many errors; never loop forever


class ScpiInstrument(BaseInstrument):
    """A SCPI-speaking instrument behind a transport."""

    def __init__(
        self,
        transport: "BaseTransport",
        device: "Device | None" = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(device)
        self.transport = transport
        self.device = device
        self.timeout = timeout

    # -- core io ------------------------------------------------------------

    def write(self, command: str) -> None:
        """Send a setting command. Checked against the device safety policy."""
        if self.device is not None:
            self.device.check_command(command)
        self.transport.write(command)

    def query(self, command: str, timeout: float | None = None) -> str:
        """Send a query (should end in '?') and return the response."""
        if self.device is not None:
            self.device.check_command(command)
        return self.transport.query(
            command, timeout=timeout if timeout is not None else self.timeout
        )

    def execute(self, command: str, timeout: float | None = None) -> str | None:
        """Send a command, auto-detecting queries by a trailing '?'.

        This is the agent-friendly entry point: one call, either result.
        """
        if command.strip().endswith("?"):
            return self.query(command, timeout=timeout)
        self.write(command)
        return None

    # -- IEEE-488.2 common commands -----------------------------------------

    def idn(self) -> str:
        """*IDN? — identity string, e.g. 'KEITHLEY,2400,123456,C33'."""
        return self.query("*IDN?")

    def identify(self) -> dict[str, str]:
        """*IDN? split into manufacturer/model/serial/firmware fields."""
        parts = [p.strip() for p in self.idn().split(",")]
        while len(parts) < 4:
            parts.append("")
        return {
            "manufacturer": parts[0],
            "model": parts[1],
            "serial": parts[2],
            "firmware": parts[3],
        }

    def reset(self) -> None:
        """*RST — reset the instrument to a known state."""
        self.write("*RST")

    def opc(self, timeout: float | None = None) -> bool:
        """*OPC? — True when all pending operations have completed."""
        reply = self.query("*OPC?", timeout=timeout)
        return reply.strip() == "1"

    def wait_until_done(self, timeout: float = 30.0, poll: float = 0.1) -> None:
        """Poll *OPC? until the instrument is idle, or raise TimeoutError_."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.opc(timeout=max(poll, 1.0)):
                    return
            except TimeoutError_:
                pass  # long operation still running — keep polling
            time.sleep(poll)
        raise TimeoutError_(
            f"Instrument still busy after {timeout}s (*OPC? never returned 1)."
        )

    # -- error queue -----------------------------------------------------------

    def read_error(self) -> tuple[int, str]:
        """Read one entry from SYSTem:ERR?. Returns (code, message)."""
        raw = self.query("SYST:ERR?")
        match = re.match(r"\s*(-?\d+)\s*,\s*\"?(.*?)\"?\s*$", raw)
        if not match:
            return 0, raw
        return int(match.group(1)), match.group(2)

    def drain_errors(self) -> list[dict[str, object]]:
        """Read the whole error queue. Stops at code 0 ('no error')."""
        errors: list[dict[str, object]] = []
        for _ in range(MAX_QUEUE_READS):
            code, message = self.read_error()
            if code == 0:
                break
            errors.append({"code": code, "message": message})
        return errors

    # -- session helpers ---------------------------------------------------------

    def safe_execute(self, command: str, timeout: float | None = None) -> dict[str, object]:
        """Execute one command and report result + instrument errors, structured.

        Designed for agents: always returns a dict, never raises on SCPI-level
        errors (transport failures still raise).
        """
        started = time.monotonic()
        result: dict[str, object] = {
            "command": command,
            "ok": True,
            "query": command.strip().endswith("?"),
        }
        try:
            result["response"] = self.execute(command, timeout=timeout)
        except InstrumentError as exc:
            result["ok"] = False
            result["error"] = str(exc)
            return result
        result["errors"] = self.drain_errors()
        if result["errors"]:
            result["ok"] = False
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        return result

    def close(self) -> None:
        """Close the underlying transport."""
        self.transport.close()
