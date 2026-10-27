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
    if addr.scheme == "logic":
        # A logic-analyzer capture daemon: JSON-RPC framed over a plain socket
        # by hil.drivers.logic, so the byte-pipe is just TCP.
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
    """Report which optional backends are usable right now (for `hil doctor`).

    ``usb`` means "USB-TMC instruments are reachable". USB-TMC goes through
    PyVISA in HIL, so a working ``pyvisa`` satisfies it — the standalone
    ``usbtmc`` package is an alternative, not a requirement. Reporting ``usb:
    False`` while ``visa`` works (the old behaviour) mislabelled a fully
    functional USB-TMC path as missing.
    """
    status = {"tcp": True, "serial": False, "visa": False, "usb": False, "logic": True}
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
    # USB-TMC works through PyVISA even without the standalone usbtmc package.
    if status["visa"]:
        status["usb"] = True
    return status


def backend_details() -> dict[str, str]:
    """Human-readable capability notes per backend, for `hil doctor` hints.

    Distinguishes "pyvisa present" from "pyvisa-py (pure-Python USB-TMC) present"
    so the doctor line can tell an agent exactly which library unlocks USB-TMC —
    with a concrete, copy-pasteable install command targeting THIS interpreter
    (uv-tool and uv venvs have no ``pip``; see :mod:`hil.pyinstall`).
    """
    from ..pyinstall import install_command

    details: dict[str, str] = {}
    try:
        import pyvisa  # type: ignore[import-not-found]

        details["visa"] = f"pyvisa {getattr(pyvisa, '__version__', '?')}"
    except ImportError:
        details["visa"] = "missing — install with: " + install_command(("pyvisa", "pyvisa-py"))
    try:
        import pyvisa_py  # type: ignore[import-not-found]  # noqa: F401

        details["usb-tmc"] = "pyvisa-py present — USB-TMC reachable via visa://"
    except ImportError:
        details["usb-tmc"] = (
            "pyvisa-py missing — pure-Python USB-TMC needs it. Install with: "
            + install_command(("pyvisa-py", "pyusb"))
        )
    return details


__all__ = [
    "Transport",
    "BaseTransport",
    "TcpTransport",
    "create_transport",
    "backend_status",
    "backend_details",
]
