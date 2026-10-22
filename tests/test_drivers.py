"""Tests for the driver abstraction and the non-SCPI PPK2 driver.

The PPK2 backend is injected as a fake, so these run with no hardware and no
``ppk2-api`` install — exactly how a third-party driver should be testable.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hil.addresses import Address, parse_address
from hil.drivers import available_drivers, get_driver, register_driver
from hil.drivers.ppk2 import Ppk2Instrument, ppk2_driver
from hil.errors import HilError, SafetyViolationError
from hil.registry import load_registry
from hil.registry.schema import SafetyPolicy
from hil.telemetry import Sample


class FakeBackend:
    """A scripted PPK2 backend (µA samples) standing in for ppk2-api."""

    def __init__(self, samples_ua=None):
        self.samples = list(samples_ua if samples_ua is not None else [50000.0])
        self.vset = 0
        self.mode = None
        self.measuring = False
        self.closed = False
        self.dio_dir = [False] * 8
        self.dio = [0] * 8
        self.log: list = []

    def connect(self): self.log.append("connect")
    def use_source_meter(self): self.mode = "source"; self.log.append("source")
    def use_amp_meter(self): self.mode = "amp"; self.log.append("amp")
    def set_source_voltage(self, mv): self.vset = int(mv); self.log.append(("vset", mv))
    def start(self): self.measuring = True; self.log.append("start")
    def stop(self): self.measuring = False; self.log.append("stop")
    def read_samples(self): return list(self.samples)
    def close(self): self.closed = True
    def info(self): return "fake"

    # -- 8 digital I/O channels --------------------------------------------
    def set_digital_direction(self, pin, output):
        self.dio_dir[pin] = bool(output); self.log.append(("dir", pin, output))
    def write_digital(self, pin, value):
        self.dio[pin] = 1 if value else 0; self.log.append(("dio", pin, value))
    def write_digital_port(self, value):
        value &= 0xFF
        self.dio = [(value >> i) & 1 for i in range(8)]; self.log.append(("port", value))
    def read_digital(self):
        return sum(b << i for i, b in enumerate(self.dio))


def test_driver_registry_has_builtins():
    names = available_drivers()
    assert "scpi" in names and "ppk2" in names
    assert callable(get_driver("scpi")) and callable(get_driver("ppk2"))


def test_driver_registry_rejects_unknown():
    with pytest.raises(HilError):
        get_driver("does-not-exist")


def test_driver_registry_third_party_registration():
    sentinel = object()
    register_driver("mypsu", lambda *a: sentinel)
    assert get_driver("mypsu") is not None
    assert "mypsu" in available_drivers()


def test_registry_profile_declares_ppk2_driver():
    dev = load_registry()["nordic-ppk2"]
    assert dev.driver == "ppk2"
    assert "measure-power" in dev.capabilities
    assert any(c.syntax == ":MEAS:CURR?" for c in dev.commands)


def test_ppk2_address_parsing():
    assert parse_address("ppk2:///dev/ttyACM0").path == "/dev/ttyACM0"
    assert parse_address("ppk2://COM3").path == "COM3"


def test_ppk2_pseudo_scpi_facade():
    be = FakeBackend(samples_ua=[50000.0])  # 50 mA
    inst = Ppk2Instrument("COM3", backend=be)
    inst.write(":CONF:MODE SOUR")
    assert be.mode == "source"
    inst.write(":SOUR:VOLT 3300")
    assert be.vset == 3300
    assert inst.query(":SOUR:VOLT?") == "3300"
    inst.write(":INIT:ACQ")
    assert be.measuring is True
    assert float(inst.query(":MEAS:CURR?")) == pytest.approx(0.05)        # 50000 µA -> A
    assert float(inst.query(":MEAS:POWer?")) == pytest.approx(0.165)      # 0.05 A * 3.3 V
    assert inst.idn().startswith("Nordic,PPK2,COM3")
    assert inst.identify()["manufacturer"] == "Nordic"
    inst.write(":MEAS:STOP")
    assert be.measuring is False
    inst.close()
    assert be.closed is True


def test_ppk2_unsupported_command_raises():
    inst = Ppk2Instrument("COM3", backend=FakeBackend())
    with pytest.raises(HilError):
        inst.write(":BOGUS:THING 1")
    with pytest.raises(HilError):
        inst.query(":BOGUS?")


def test_ppk2_safety_policy_enforced():
    dev = load_registry()["nordic-ppk2"]
    strict = replace(dev, safety=SafetyPolicy(blocked=(":SOUR:VOLT",)))
    inst = Ppk2Instrument("COM3", device=strict, backend=FakeBackend())
    with pytest.raises(SafetyViolationError):
        inst.write(":SOUR:VOLT 3300")


def test_ppk2_capture_streams_timestamped_samples():
    be = FakeBackend(samples_ua=[10000.0, 20000.0, 30000.0])
    inst = Ppk2Instrument("COM3", backend=be)
    inst.write(":SOUR:VOLT 5000")
    samples = inst.capture("current", seconds=0.15, interval=0.05, units="A")
    assert samples and all(isinstance(s, Sample) for s in samples)
    assert all(s.units == "A" for s in samples)
    assert {round(s.value, 6) for s in samples} <= {0.01, 0.02, 0.03}


def test_ppk2_capture_power_channel():
    be = FakeBackend(samples_ua=[100000.0])  # 0.1 A
    inst = Ppk2Instrument("COM3", backend=be)
    inst.write(":SOUR:VOLT 5000")           # 5 V
    samples = inst.capture("power", seconds=0.1, interval=0.05)
    assert samples and samples[0].units == "W"
    assert samples[0].value == pytest.approx(0.5)  # 0.1 A * 5 V


def test_ppk2_driver_requires_port():
    with pytest.raises(HilError):
        ppk2_driver(Address(scheme="ppk2", path=""), None, 5.0)


def test_ppk2_digital_facade():
    be = FakeBackend()
    inst = Ppk2Instrument("COM3", backend=be)
    inst.write(":DIG0:DIR OUT")
    assert be.dio_dir[0] is True
    inst.write(":DIG0 ON")
    assert inst.query(":DIG0?") == "1"
    inst.write(":DIG1 1")
    assert inst.query(":DIG:PORT?") == "3"        # bits 0 and 1 set
    inst.write(":DIG:PORT 0")
    assert inst.query(":DIG:PORT?") == "0"
    inst.write(":DIG7:DIR IN")
    assert be.dio_dir[7] is False


def test_ppk2_digital_pin_bounds():
    inst = Ppk2Instrument("COM3", backend=FakeBackend())
    with pytest.raises(HilError):
        inst.write(":DIG9 ON")                     # only 0..7 exist
    with pytest.raises(HilError):
        inst.query(":DIG8?")


def test_ppk2_capture_digital_channel():
    be = FakeBackend()
    inst = Ppk2Instrument("COM3", backend=be)
    inst.write(":DIG:PORT 5")
    samples = inst.capture("digital", seconds=0.12, interval=0.04)
    assert samples and all(s.units == "port" for s in samples)
    assert all(s.value == 5 for s in samples)
    assert be.measuring is False                   # digital capture does not start current


def test_ppk2_api_backend_digital_unsupported_raises():
    from hil.drivers.ppk2 import Ppk2ApiBackend

    be = Ppk2ApiBackend("COM3")
    be._api = object()                             # bypass lazy ppk2-api import
    with pytest.raises(HilError):
        be.read_digital()
    with pytest.raises(HilError):
        be.write_digital(0, True)
