"""HIL — Hardware-in-the-Loop instrument testing for Python, CLI, and agents.

    from hil import connect

    smu = connect("tcp://192.168.1.5:5025")          # by address
    smu = connect("keithley-2400")                   # by registry slug
    print(smu.idn())                                 # 'KEITHLEY,2400,...'
    smu.write(":SOUR:VOLT 3.3")
    print(smu.query(":MEAS:VOLT?"))
"""

from __future__ import annotations

from pathlib import Path

from . import addresses, drivers, errors, instrument, registry, scpi, telemetry, transports
from .addresses import Address, parse_address
from .errors import HilError
from .instrument import BaseInstrument, Instrument
from .registry import Device, load_registry
from .scpi import ScpiInstrument
from .telemetry import DataLogger, Sample, summarize
from .transports import BaseTransport, TcpTransport, create_transport

__version__ = "0.1.0"

__all__ = [
    "connect",
    "resolve_target",
    "parse_address",
    "Address",
    "HilError",
    "Instrument",
    "BaseInstrument",
    "ScpiInstrument",
    "Device",
    "load_registry",
    "Sample",
    "DataLogger",
    "summarize",
    "TcpTransport",
    "BaseTransport",
    "addresses",
    "drivers",
    "errors",
    "instrument",
    "registry",
    "scpi",
    "telemetry",
    "transports",
]


def resolve_target(
    target: str,
    devices: dict[str, Device] | None = None,
) -> tuple[Address, Device | None]:
    """Resolve an address-or-slug into (Address, optional Device policy).

    Slugs may carry a registry ``default_address``; bare addresses get no
    device policy unless it can be matched trivially.
    """
    if "://" in target or "::" in target:
        return parse_address(target), None

    devices = devices if devices is not None else load_registry()
    if target in devices:
        device = devices[target]
        if device.default_address:
            return parse_address(device.default_address), device
        examples = (
            "; ".join(device.connection_examples) or "no examples recorded"
        )
        raise errors.AddressError(
            f"Device '{target}' has no default address. Connect explicitly, "
            f"e.g.: hil idn '{(device.connection_examples[0] if device.connection_examples else 'tcp://<ip>:5025')}' "
            f"— recorded examples: {examples}."
        )
    raise errors.AddressError(
        f"'{target}' is neither an address (no '://') nor a known device slug. "
        f"See 'hil devices list'."
    )


def connect(
    target: str,
    *,
    device_slug: str | None = None,
    timeout: float = 5.0,
    registry_dirs: tuple[Path, ...] = (),
) -> Instrument:
    """Connect to an instrument by address or registry slug.

    Returns an opened :class:`~hil.instrument.Instrument`. The concrete driver is
    chosen from the device profile's ``driver:`` field (default ``scpi``) or the
    address scheme (``ppk2://`` selects the PPK2 driver). When connecting by
    address, pass ``device_slug`` to attach that device's safety policy.
    """
    devices = load_registry(registry_dirs) if registry_dirs else None
    addr, device = resolve_target(target, devices)
    if device_slug is not None:
        devices = devices if devices is not None else load_registry()
        if device_slug not in devices:
            raise errors.DeviceNotFoundError(
                f"Unknown device slug '{device_slug}'. See 'hil devices list'."
            )
        device = devices[device_slug]

    driver = (getattr(device, "driver", "scpi") or "scpi")
    if addr.scheme == "ppk2":
        driver = "ppk2"
    return drivers.get_driver(driver)(addr, device, timeout)
