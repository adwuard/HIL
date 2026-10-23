"""Pluggable instrument drivers.

A *driver* is a factory ``(addr, device, timeout) -> Instrument``. The registry
maps a name to a factory so ``connect()`` can build the right instrument from an
address scheme or a device profile's ``driver:`` field.

Built-ins: ``scpi`` (default), ``ppk2`` and ``logic``. Third parties register more
**without editing HIL** via the ``hil.drivers`` entry-point group::

    # in your package's pyproject.toml
    [project.entry-points."hil.drivers"]
    mypsu = "mypkg.hil_driver:factory"

Then ``connect("mypsu://...")`` (or a device with ``driver: mypsu``) just works.
"""

from __future__ import annotations

from typing import Any, Callable

from ..errors import HilError

DriverFactory = Callable[..., Any]

_DRIVERS: dict[str, DriverFactory] = {}
_DISCOVERED = False
_BUILTIN = ("scpi", "ppk2", "logic")


def register_driver(name: str, factory: DriverFactory) -> None:
    """Register (or replace) a driver factory under ``name``."""
    _DRIVERS[name.lower()] = factory


def _load_builtin(name: str) -> None:
    if name == "scpi":
        from .scpi import scpi_driver
        register_driver("scpi", scpi_driver)
    elif name == "ppk2":
        from .ppk2 import ppk2_driver
        register_driver("ppk2", ppk2_driver)
    elif name == "logic":
        from .logic import logic_driver
        register_driver("logic", logic_driver)


def _discover() -> None:
    """Merge third-party drivers from the ``hil.drivers`` entry-point group."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = (
            eps.select(group="hil.drivers")
            if hasattr(eps, "select")
            else list(eps.get("hil.drivers", []))  # type: ignore[attr-defined]
        )
    except Exception:  # pragma: no cover - entry points are best-effort
        group = []
    for ep in group:
        try:
            register_driver(ep.name, ep.load())
        except Exception:  # pragma: no cover - a broken plugin must not break HIL
            pass


def get_driver(name: str) -> DriverFactory:
    """Return the factory for ``name`` (loading built-ins / plugins as needed)."""
    name = (name or "scpi").lower()
    if name not in _DRIVERS:
        _load_builtin(name)
    if name not in _DRIVERS:
        _discover()
    if name not in _DRIVERS:
        raise HilError(
            f"unknown instrument driver '{name}' (built-ins: {', '.join(_BUILTIN)}; "
            "third parties register via the 'hil.drivers' entry point)"
        )
    return _DRIVERS[name]


def available_drivers() -> list[str]:
    """All registered + built-in + discovered driver names."""
    _discover()
    return sorted(set(_DRIVERS) | set(_BUILTIN))
