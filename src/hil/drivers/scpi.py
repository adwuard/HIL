"""Built-in SCPI driver (the default): address -> Transport -> ScpiInstrument."""

from __future__ import annotations

from ..addresses import Address
from ..registry import Device
from ..scpi import ScpiInstrument
from ..transports import create_transport


def scpi_driver(addr: Address, device: Device | None, timeout: float) -> ScpiInstrument:
    """Build a :class:`~hil.scpi.ScpiInstrument` over an opened transport."""
    transport = create_transport(addr, timeout=timeout)
    transport.open()
    return ScpiInstrument(transport, device=device, timeout=timeout)
