from __future__ import annotations

import textwrap

import pytest

from hil.errors import RegistryError, SafetyViolationError
from hil.registry import load_device_file, load_registry
from hil.registry.loader import BUILTIN_DIR


def test_builtin_registry_loads():
    devices = load_registry()
    slugs = set(devices)
    assert {"keithley-2400", "rigol-dho1072", "keysight-ac6801b", "ea-el9000b"} <= slugs
    smu = devices["keithley-2400"]
    assert smu.manufacturer == "Keithley"
    assert "source-dc-voltage" in smu.capabilities
    assert any(c.syntax == ":SOUR:VOLT <v>" for c in smu.commands)


def test_logic_analyzer_device_routes_to_logic_driver():
    devices = load_registry()
    la = devices["alientek-atk-logic"]
    assert la.driver == "logic" and la.category == "logic-analyzer"
    assert {"logic-capture", "protocol-decode", "stream-to-disk"} <= set(la.capabilities)
    # the pseudo-SCPI facade verbs the driver answers are all declared
    heads = {c.syntax.split()[0] for c in la.commands if c.syntax}
    assert ":FETCh?" in heads and ":DEC" in heads and ":ACQ:ARM" in heads


def test_debug_target_devices_route_to_debug_driver():
    devices = load_registry()
    mcu = devices["generic-cortex-m"]
    assert mcu.driver == "debug" and mcu.category == "mcu-debug-target"
    assert {"debug-probe", "debug-flash", "debug-control", "debug-rtt"} <= set(mcu.capabilities)
    esp = devices["esp32-devkit"]
    assert esp.driver == "debug" and "debug-flash" in esp.capabilities
    # the pseudo-SCPI facade verbs the debug driver answers are all declared
    heads = {c.syntax.split()[0] for c in mcu.commands if c.syntax}
    assert {":FLASH", ":HALT", ":MEM:READ?", ":REG:READ?", ":RTT", ":GDB"} <= heads


def test_safety_blocked():
    devices = load_registry()
    smu = devices["keithley-2400"]
    smu.check_command(":SOUR:VOLT 10")  # fine
    with pytest.raises(SafetyViolationError):
        smu.check_command("CAL:PROT:CODE 12345")


def test_safety_allow_list_deny_by_default():
    devices = load_registry()
    ac = devices["keysight-ac6801b"]
    ac.check_command("*IDN?")  # allowed
    ac.check_command("MEAS:VOLT?")  # allowed
    with pytest.raises(SafetyViolationError):
        ac.check_command("OUTP ON")  # not on allow-list


def test_command_head_extraction():
    devices = load_registry()
    smu = devices["keithley-2400"]
    # only the command head is matched; parameters never hit the policy regex
    smu.check_command(":SOUR:VOLT 3.3")


def test_invalid_yaml_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            manufacturer: X
            model: Y
            # slug missing
            """
        )
    )
    with pytest.raises(RegistryError):
        load_device_file(bad)


def test_bad_slug_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("slug: Not_A_Slug\nmanufacturer: X\nmodel: Y\n")
    with pytest.raises(RegistryError):
        load_device_file(bad)


def test_user_dir_overrides_builtin(tmp_path):
    override = tmp_path / "keithley-2400.yaml"
    override.write_text(
        textwrap.dedent(
            """
            slug: keithley-2400
            manufacturer: Keithley
            model: 2400-UPDATED
            category: source-measure-unit
            """
        )
    )
    devices = load_registry((tmp_path,))
    assert devices["keithley-2400"].model == "2400-UPDATED"


def test_builtin_dir_exists():
    assert BUILTIN_DIR.is_dir()
    assert len(list(BUILTIN_DIR.glob("*.yaml"))) >= 4
