"""Transport registry and factory: address -> open transport.

Core transports (TCP) are stdlib and always available; serial and VISA are
lazily imported behind their extras so the base install stays light.
"""

from __future__ import annotations

from ..addresses import Address
from .base import BaseTransport, Transport
from .tcp import TcpTransport


def create_transport(addr: Address, timeout: float = 5.0) -> BaseTransport:
    """Build (but do not open) the transport for a parsed address."""
    if addr.scheme == "tcp":
        return TcpTransport(addr.host, addr.port, timeout=timeout)
    if addr.scheme == "serial":
        from .serial import SerialTransport

        return SerialTransport(
            addr.path,
            baud=int(addr.params.get("baud", "9600")),
            timeout=timeout,
        )
    if addr.scheme == "visa":
        from .visa import VisaTransport

        return VisaTransport(addr.path, timeout=timeout)
    raise ValueError(f"Unsupported scheme: {addr.scheme}")  # pragma: no cover


def backend_status() -> dict[str, bool]:
    """Report which optional backends are importable right now (for `hil doctor`)."""
    status = {"tcp": True, "serial": False, "visa": False, "usb": False}
    try:
        import serial  # noqa: F401

        status["serial"] = True
    except ImportError:
        pass
    try:
        import pyvisa  # noqa: F401

        status["visa"] = True
    except ImportError:
        pass
    try:
        import usbtmc  # noqa: F401

        status["usb"] = True
    except ImportError:
        pass
    return status


__all__ = [
    "Transport",
    "BaseTransport",
    "TcpTransport",
    "create_transport",
    "backend_status",
]
