"""Connection address parsing.

Supported address forms:

    tcp://host[:port]                 raw SCPI socket (default port 5025)
    serial://PORT[?baud=9600]         RS-232, e.g. serial:///dev/ttyUSB0?baud=9600
    visa://VISA_RESOURCE              any VISA resource, e.g. visa://GPIB::24::INSTR
    ppk2://PORT                       Nordic Power Profiler Kit II (non-SCPI driver)
    GPIB::24::INSTR                   bare VISA resource (auto-detected by '::')

A bare word that is not an address (e.g. ``keithley-2400``) is treated as a
device-registry slug by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import AddressError

DEFAULT_SCPI_PORT = 5025
DEFAULT_BAUD = 9600

SUPPORTED_SCHEMES = ("tcp", "serial", "visa", "ppk2")


@dataclass(frozen=True)
class Address:
    """A parsed instrument address."""

    scheme: str
    """Transport scheme: 'tcp', 'serial', 'visa', or 'ppk2'."""

    host: str = ""
    """Remote host (tcp)."""

    port: int = 0
    """Remote port (tcp)."""

    path: str = ""
    """Serial port path or VISA resource string."""

    params: dict[str, str] = field(default_factory=dict)
    """Query parameters (serial baud etc.)."""

    def __str__(self) -> str:  # pragma: no cover - display only
        if self.scheme == "tcp":
            return f"tcp://{self.host}:{self.port}"
        if self.scheme == "serial":
            qs = "".join(f"&{k}={v}" for k, v in self.params.items())
            return f"serial://{self.path}{qs}"
        if self.scheme == "ppk2":
            return f"ppk2://{self.path}"
        return f"visa://{self.path}"


def parse_address(raw: str) -> Address:
    """Parse an address string into an :class:`Address`.

    Raises :class:`AddressError` on malformed input.
    """
    s = raw.strip()
    if not s:
        raise AddressError("Empty address. Expected e.g. 'tcp://192.168.1.5:5025'.")

    # Bare VISA resource string, e.g. "GPIB::24::INSTR" or "TCPIP::host::INSTR"
    if "::" in s and "://" not in s:
        return Address(scheme="visa", path=s)

    if "://" not in s:
        raise AddressError(
            f"Unrecognized address '{raw}'. Use a scheme like 'tcp://host:5025', "
            f"'serial:///dev/ttyUSB0?baud=9600', 'visa://GPIB::24::INSTR', "
            f"or a device slug from 'hil devices list'."
        )

    scheme, _, rest = s.partition("://")
    scheme = scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise AddressError(
            f"Unknown scheme '{scheme}://'. Supported: "
            + ", ".join(f"{k}://" for k in SUPPORTED_SCHEMES)
            + " or a bare VISA resource like 'GPIB::24::INSTR'."
        )

    if scheme == "tcp":
        parts = urlsplit(f"//{rest}", scheme="tcp")
        host = parts.hostname or ""
        if not host:
            raise AddressError(f"Missing host in tcp address '{raw}'.")
        return Address(scheme="tcp", host=host, port=parts.port or DEFAULT_SCPI_PORT)

    # serial:// and visa:// keep the remainder verbatim (minus query for serial)
    if scheme == "serial":
        split = urlsplit(f"path://unused{rest if rest.startswith('/') else '/' + rest}")
        path = unquote(split.path)
        if rest.startswith("?"):  # e.g. serial://?baud=9600 with no port — invalid
            raise AddressError(f"Missing serial port in address '{raw}'.")
        params = {k: v[-1] for k, v in parse_qs(split.query).items()}
        if "baud" in params:
            try:
                int(params["baud"])
            except ValueError as exc:
                raise AddressError(
                    f"Invalid baud rate '{params['baud']}' in '{raw}'."
                ) from exc
        else:
            params["baud"] = str(DEFAULT_BAUD)
        return Address(scheme="serial", path=path, params=params)

    if scheme == "ppk2":
        # ppk2://PORT — a serial port path (COM3, /dev/ttyACM0, /dev/cu.usbmodem*)
        if not rest:
            raise AddressError(f"Missing serial port in ppk2 address '{raw}'.")
        return Address(scheme="ppk2", path=unquote(rest))

    # visa:// — everything after the scheme is the resource string
    if not rest:
        raise AddressError(f"Missing VISA resource in address '{raw}'.")
    return Address(scheme="visa", path=unquote(rest))
