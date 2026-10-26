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
        self.dio_in = 0   # 8-bit digital input word (D0..D7); inputs only
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

    # -- 8 digital input channels (read-only) ------------------------------
    def set_digital_input(self, value):
        self.dio_in = int(value) & 0xFF
    def read_digital(self):
        return self.dio_in


class FakePpk2Api:
    """Mimics ppk2-api 0.9.2's PPK2_API surface for backend-level tests.

    Models the real contract discovered on hardware: ``get_samples()`` returns
    ``(analog_uA, digital_words)``, AMP mode is ``use_ampere_meter``, DUT power
    is ``toggle_DUT_power("ON"/"OFF")``, and ``start_measuring()`` requires a
    voltage to have been set first.
    """

    def __init__(self, analog=(1000.0, 2000.0), digital=(0b101, 0b011)):
        self._analog = list(analog)
        self._digital = list(digital)
        self.mode = None
        self.vdd = None
        self.measuring = False
        self.dut = None
        self.modifiers = {"Calibrated": None}
        self.ser = self                     # _drain() calls ser.reset_input_buffer()

    def reset_input_buffer(self): pass
    def reset_output_buffer(self): pass
    def get_modifiers(self): self.modifiers["Calibrated"] = True; return True
    def use_source_meter(self): self.mode = "SOURCE_MODE"
    def use_ampere_meter(self): self.mode = "AMPERE_MODE"
    def set_source_voltage(self, mV): self.vdd = int(mV)
    def start_measuring(self):
        if not self.vdd:
            raise Exception("Input voltage not set!")
        self.measuring = True
    def stop_measuring(self): self.measuring = False
    def toggle_DUT_power(self, state): self.dut = state
    def get_data(self): return b"\x01\x02\x03\x04" if self.measuring else b""
    def get_samples(self, buf): return (list(self._analog), list(self._digital))


def test_driver_registry_has_builtins():
    names = available_drivers()
    assert {"scpi", "ppk2", "logic", "debug"} <= set(names)
    assert callable(get_driver("scpi")) and callable(get_driver("ppk2"))
    assert callable(get_driver("logic")) and callable(get_driver("debug"))


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


def test_ppk2_digital_input_read():
    be = FakeBackend()
    inst = Ppk2Instrument("COM3", backend=be)
    be.set_digital_input(0b00000101)               # D0 and D2 high
    assert inst.query(":DIG:PORT?") == "5"
    assert inst.query(":DIG0?") == "1"
    assert inst.query(":DIG1?") == "0"
    assert inst.query(":DIG2?") == "1"


def test_ppk2_digital_channels_are_input_only():
    inst = Ppk2Instrument("COM3", backend=FakeBackend())
    with pytest.raises(HilError):                  # no output drive on the PPK2
        inst.write(":DIG0 ON")
    with pytest.raises(HilError):
        inst.write(":DIG:PORT 255")
    with pytest.raises(HilError):
        inst.write(":DIG0:DIR OUT")


def test_ppk2_digital_pin_bounds():
    inst = Ppk2Instrument("COM3", backend=FakeBackend())
    with pytest.raises(HilError):
        inst.query(":DIG8?")                       # only 0..7 exist


def test_ppk2_capture_digital_channel():
    be = FakeBackend()
    inst = Ppk2Instrument("COM3", backend=be)
    be.set_digital_input(5)
    samples = inst.capture("digital", seconds=0.12, interval=0.04)
    assert samples and all(s.units == "input" for s in samples)
    assert all(s.value == 5 for s in samples)
    assert be.measuring is False                   # digital capture does not start current


def test_ppk2_api_backend_digital_unsupported_raises():
    from hil.drivers.ppk2 import Ppk2ApiBackend

    be = Ppk2ApiBackend("COM3")
    be._api = object()                             # bypass lazy ppk2-api import
    with pytest.raises(HilError):
        be.read_digital()


# -- ppk2-api 0.9.2 contract (validated against real PPK2 hardware) ----------


def test_ppk2_api_backend_unpacks_tuple_samples():
    # ppk2-api >=0.9 get_samples() -> (analog_uA, digital_words); the old driver
    # did float(x) over the 2-tuple and crashed. Regression: unpack the analog.
    from hil.drivers.ppk2 import Ppk2ApiBackend
    be = Ppk2ApiBackend("COM3"); api = FakePpk2Api(analog=(1500.0, 2500.0)); be._api = api
    api.measuring = True
    assert be.read_samples() == [1500.0, 2500.0]


def test_ppk2_api_backend_legacy_flat_samples():
    # Older ppk2-api returned a flat list of floats; both shapes must work.
    from hil.drivers.ppk2 import Ppk2ApiBackend
    class Legacy(FakePpk2Api):
        def get_samples(self, buf): return list(self._analog)
    be = Ppk2ApiBackend("COM3"); api = Legacy(analog=(999.0,)); be._api = api
    api.measuring = True
    assert be.read_samples() == [999.0]


def test_ppk2_api_backend_read_digital_decodes_stream():
    # D0..D7 ride inside each sample frame; a read returns the latest captured
    # 8-bit word (the driver used to report these as unsupported).
    from hil.drivers.ppk2 import Ppk2ApiBackend
    be = Ppk2ApiBackend("COM3"); api = FakePpk2Api(digital=(0b101, 0b011)); be._api = api
    api.measuring = True
    assert be.read_digital() == 0b011


def test_ppk2_api_backend_read_digital_needs_acquisition():
    from hil.drivers.ppk2 import Ppk2ApiBackend
    be = Ppk2ApiBackend("COM3"); api = FakePpk2Api(); be._api = api
    assert api.measuring is False                  # get_data() -> b"" when idle
    with pytest.raises(HilError):
        be.read_digital()


def test_ppk2_api_backend_amp_alias_maps_to_use_ampere_meter():
    from hil.drivers.ppk2 import Ppk2ApiBackend
    be = Ppk2ApiBackend("COM3"); api = FakePpk2Api(); be._api = api
    be.use_amp_meter()
    assert api.mode == "AMPERE_MODE"


def test_ppk2_api_backend_set_output_toggles_dut_power():
    from hil.drivers.ppk2 import Ppk2ApiBackend
    be = Ppk2ApiBackend("COM3"); api = FakePpk2Api(); be._api = api
    be.set_output(True);  assert api.dut == "ON"
    be.set_output(False); assert api.dut == "OFF"


def test_ppk2_api_backend_calibrate_tolerates_stale_buffer():
    # A device left streaming corrupts the UTF-8 metadata scan; _calibrate must
    # drain + retry rather than let UnicodeDecodeError crash connect().
    from hil.drivers.ppk2 import Ppk2ApiBackend
    class Flaky(FakePpk2Api):
        def __init__(self): super().__init__(); self._n = 0
        def get_modifiers(self):
            self._n += 1
            if self._n < 3:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            self.modifiers["Calibrated"] = True
            return True
    be = Ppk2ApiBackend("COM3"); api = Flaky(); be._api = api
    be._calibrate()
    assert api.modifiers["Calibrated"] is True


def test_ppk2_measure_without_voltage_raises_actionable():
    # The PPK2 needs a voltage reference before sampling; surface an actionable
    # error instead of ppk2-api's opaque "Input voltage not set!".
    inst = Ppk2Instrument("COM3", backend=FakeBackend())
    with pytest.raises(HilError, match="SOUR:VOLT"):
        inst.query(":MEAS:CURR?")


def test_ppk2_outp_drives_dut_power_when_backend_supports():
    class FakeWithOutput(FakeBackend):
        def __init__(self): super().__init__(); self.out = None
        def set_output(self, on): self.out = bool(on)
    be = FakeWithOutput()
    inst = Ppk2Instrument("COM3", backend=be)
    inst.write(":OUTP ON")
    assert be.out is True and be.mode == "source"
    inst.write(":OUTP OFF")
    assert be.out is False and be.measuring is False
    # closing always de-energizes (a failed procedure skips its ':OUTP OFF')
    be.out = True                      # simulate output left sourcing
    inst.close()
    assert be.out is False
