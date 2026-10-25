from __future__ import annotations

import pytest

from hil.addresses import parse_address
from hil.errors import AddressError


def test_tcp_with_port():
    addr = parse_address("tcp://192.168.1.5:5025")
    assert addr.scheme == "tcp"
    assert addr.host == "192.168.1.5"
    assert addr.port == 5025


def test_tcp_default_port():
    addr = parse_address("tcp://scope.local")
    assert addr.port == 5025


def test_serial_with_params():
    addr = parse_address("serial:///dev/ttyUSB0?baud=115200")
    assert addr.scheme == "serial"
    assert addr.path == "/dev/ttyUSB0"
    assert addr.params["baud"] == "115200"


def test_serial_default_baud():
    addr = parse_address("serial:///dev/tty.usbserial-XYZ")
    assert addr.params["baud"] == "9600"


def test_visa_scheme():
    addr = parse_address("visa://GPIB::24::INSTR")
    assert addr.scheme == "visa"
    assert addr.path == "GPIB::24::INSTR"


def test_bare_visa_resource():
    addr = parse_address("TCPIP::192.168.1.5::INSTR")
    assert addr.scheme == "visa"
    assert addr.path == "TCPIP::192.168.1.5::INSTR"


def test_logic_with_port():
    addr = parse_address("logic://127.0.0.1:5555")
    assert addr.scheme == "logic"
    assert addr.host == "127.0.0.1"
    assert addr.port == 5555
    assert str(addr) == "logic://127.0.0.1:5555"


def test_logic_default_port():
    addr = parse_address("logic://atk-daemon.local")
    assert addr.scheme == "logic"
    assert addr.host == "atk-daemon.local"
    assert addr.port == 5555


def test_debug_backend_in_host_slot():
    addr = parse_address("debug://probe-rs")
    assert addr.scheme == "debug"
    assert addr.host == "probe-rs"
    assert addr.params == {}
    assert str(addr) == "debug://probe-rs"


def test_debug_backend_is_lowercased_with_params():
    addr = parse_address("debug://Auto?target=ESP32&probe=/dev/cu.usb-1&transport=jtag")
    assert addr.scheme == "debug"
    assert addr.host == "auto"                              # backend lowercased
    assert addr.params["target"] == "ESP32"                  # param value verbatim
    assert addr.params["probe"] == "/dev/cu.usb-1"
    assert addr.params["transport"] == "jtag"


def test_debug_simulator_roundtrip():
    addr = parse_address("debug://simulator")
    assert addr.host == "simulator"
    assert str(addr) == "debug://simulator"


@pytest.mark.parametrize(
    "bad",
    ["", "no-scheme", "ftp://x", "tcp://", "logic://", "debug://",
     "serial://?baud=9600", "serial:///dev/x?baud=abc"],
)
def test_invalid_addresses(bad: str):
    with pytest.raises(AddressError):
        parse_address(bad)
