"""Python-environment introspection for backend install hints.

The single question this answers: *what is the EXACT command to install a Python
package into the interpreter running THIS ``hil``?*

Getting it wrong is expensive and was observed in real agent sessions: the
global ``hil`` is a **uv tool** install and the dev checkout is a **uv venv** —
neither ships ``pip``. An agent that reaches for ``pip install pyvisa`` fails,
then often escalates to rebuilding HIL from source (blocked, slow, wrong). The
command that actually works in both is::

    uv pip install --python /abs/path/to/that/python pyvisa pyvisa-py pyusb

So every backend hint in HIL routes through :func:`install_command`, which picks
``uv pip install --python <running-exe>`` when ``uv`` is available (or when the
interpreter has no ``pip``), and ``<running-exe> -m pip install`` otherwise. The
interpreter path is always concrete — never a ``<hil-venv>`` placeholder an
agent has to guess.

This module has **no HIL-internal imports** so any layer (transports, discovery,
provision, cli, mcp_server) can use it without an import cycle.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

# Which PyPI packages unlock each optional transport/capability. Kept here so the
# doctor, discovery warnings, `hil setup`, and the MCP hil_setup tool all agree.
BACKEND_PACKAGES: dict[str, tuple[str, ...]] = {
    "serial": ("pyserial",),
    "visa": ("pyvisa", "pyvisa-py"),
    "usb": ("pyvisa", "pyvisa-py", "pyusb"),
    "mdns": ("zeroconf",),
    "lan": ("zeroconf",),
    "all": ("pyserial", "pyvisa", "pyvisa-py", "pyusb", "zeroconf"),
}

# Human label for what each backend unlocks, used in hints.
BACKEND_LABELS: dict[str, str] = {
    "serial": "RS-232 / USB-serial instruments (serial://)",
    "visa": "VISA instruments, incl. USB-TMC (visa://)",
    "usb": "USB-TMC / GPIB instruments via PyVISA (visa://USB…)",
    "mdns": "LAN/mDNS discovery of LXI & VXI-11 instruments",
    "lan": "LAN/mDNS discovery of LXI & VXI-11 instruments",
    "all": "every optional backend (serial, VISA/USB-TMC, mDNS)",
}


def running_python() -> str:
    """Absolute path to the interpreter running hil — what installs must target."""
    return sys.executable or "python3"


def uv_available() -> bool:
    """True when the ``uv`` executable is on PATH."""
    return shutil.which("uv") is not None


def has_pip() -> bool:
    """True when ``pip`` is importable in the running interpreter."""
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def is_uv_tool_venv() -> bool:
    """True when running inside a uv *tool* venv (``…/uv/tools/<name>/bin/python``).

    Such venvs never carry ``pip``; only ``uv pip install --python <exe>`` (or
    ``uv tool install --with``) can add packages to them.
    """
    parts = [p.lower() for p in Path(running_python()).parts]
    return "uv" in parts and "tools" in parts


def install_command(packages: list[str] | tuple[str, ...]) -> str:
    """The exact shell command to install ``packages`` into the running interpreter.

    Prefers ``uv pip install --python <exe>`` whenever uv exists or pip does not —
    which is precisely the uv-tool / uv-venv case that has no pip. Falls back to
    ``<exe> -m pip install`` for a classic pip environment.
    """
    pkgs = " ".join(packages)
    exe = running_python()
    if uv_available() or not has_pip():
        return f"uv pip install --python {exe} {pkgs}"
    return f"{exe} -m pip install {pkgs}"


def backend_packages(backend: str) -> tuple[str, ...]:
    """The PyPI packages a named backend needs (empty tuple if unknown)."""
    return BACKEND_PACKAGES.get((backend or "").strip().lower(), ())


def is_backend(name: str) -> bool:
    """True when ``name`` is a python-backend target (serial/visa/usb/mdns/all)."""
    return (name or "").strip().lower() in BACKEND_PACKAGES


def backend_hint(backend: str) -> str:
    """A one-line, copy-pasteable install hint for a named backend.

    Example::

        usb: USB-TMC / GPIB instruments via PyVISA (visa://) are invisible.
        Install with: uv pip install --python /…/bin/python pyvisa pyvisa-py pyusb
    """
    key = (backend or "").strip().lower()
    pkgs = backend_packages(key)
    if not pkgs:
        return ""
    label = BACKEND_LABELS.get(key, key)
    return f"{label} — install with: {install_command(pkgs)}"


def environment_report() -> dict:
    """A snapshot of the running environment, for ``hil doctor --json``."""
    return {
        "python": running_python(),
        "uv": uv_available(),
        "pip": has_pip(),
        "uv_tool_venv": is_uv_tool_venv(),
        "install_command_example": install_command(("pyvisa",)),
    }
