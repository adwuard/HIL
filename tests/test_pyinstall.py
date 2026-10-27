"""Tests for hil.pyinstall — the exact, uv-aware backend install command.

The whole point of this module is that a hint is *never* a guess: a uv tool/venv
has no pip, so a bare ``pip install`` fails. Every branch of :func:`install_command`
must produce a command that targets the running interpreter concretely.
"""

from __future__ import annotations

import sys

from hil import pyinstall


def test_backend_packages_known_and_unknown():
    assert pyinstall.backend_packages("visa") == ("pyvisa", "pyvisa-py")
    assert pyinstall.backend_packages("serial") == ("pyserial",)
    assert "pyusb" in pyinstall.backend_packages("usb")
    assert pyinstall.backend_packages("nope") == ()
    # case- and space-insensitive
    assert pyinstall.backend_packages("  VISA ") == ("pyvisa", "pyvisa-py")


def test_is_backend():
    for name in ("serial", "visa", "usb", "mdns", "lan", "all"):
        assert pyinstall.is_backend(name)
    assert not pyinstall.is_backend("ppk2")   # a device recipe, not a python backend
    assert not pyinstall.is_backend("")


def test_install_command_prefers_uv_when_available(monkeypatch):
    monkeypatch.setattr(pyinstall, "uv_available", lambda: True)
    monkeypatch.setattr(pyinstall, "has_pip", lambda: True)
    cmd = pyinstall.install_command(("pyvisa", "pyvisa-py"))
    assert cmd == f"uv pip install --python {sys.executable} pyvisa pyvisa-py"


def test_install_command_uses_uv_when_no_pip(monkeypatch):
    # The uv-tool / uv-venv case: no uv on PATH *and* no pip. It must still avoid
    # suggesting a bare `pip install` that cannot work in this interpreter.
    monkeypatch.setattr(pyinstall, "uv_available", lambda: False)
    monkeypatch.setattr(pyinstall, "has_pip", lambda: False)
    cmd = pyinstall.install_command(("pyserial",))
    assert cmd.startswith("uv pip install --python ")
    assert cmd.endswith("pyserial")
    assert "-m pip install" not in cmd


def test_install_command_falls_back_to_pip(monkeypatch):
    monkeypatch.setattr(pyinstall, "uv_available", lambda: False)
    monkeypatch.setattr(pyinstall, "has_pip", lambda: True)
    cmd = pyinstall.install_command(("zeroconf",))
    assert cmd == f"{sys.executable} -m pip install zeroconf"


def test_backend_hint_is_copy_pasteable(monkeypatch):
    monkeypatch.setattr(pyinstall, "uv_available", lambda: True)
    hint = pyinstall.backend_hint("usb")
    assert "install with:" in hint
    assert "pyvisa" in hint and "pyusb" in hint
    assert pyinstall.backend_hint("bogus") == ""


def test_environment_report_shape(monkeypatch):
    monkeypatch.setattr(pyinstall, "uv_available", lambda: True)
    monkeypatch.setattr(pyinstall, "has_pip", lambda: False)
    rep = pyinstall.environment_report()
    assert rep["python"] == sys.executable
    assert rep["uv"] is True and rep["pip"] is False
    assert rep["install_command_example"].startswith("uv pip install --python ")


def test_is_uv_tool_venv_detection(monkeypatch):
    monkeypatch.setattr(
        pyinstall, "running_python",
        lambda: "/home/u/.local/share/uv/tools/hil/bin/python",
    )
    assert pyinstall.is_uv_tool_venv() is True
    monkeypatch.setattr(pyinstall, "running_python", lambda: "/Users/x/HIL/.venv/bin/python")
    assert pyinstall.is_uv_tool_venv() is False
