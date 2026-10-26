"""Tests for the in-process ATK-Logic backend (``logic://usb``) — no hardware, no pyusb.

A fake device feeds deterministic sample streams through the *real* vendored
``atkproto`` / ``atkdl`` / ``srdhost`` code, so the whole capture → save → load →
decode path and the :class:`~hil.drivers.logic.LogicBackend` adaptation are
exercised offline (mirroring how the daemon/sim backends are tested). The
protocol-decode tests are skipped when no sigrok decoder directory is present
(e.g. CI without an ATK-Logic install), since they need the vendor PDs.
"""

from __future__ import annotations

import pytest

from hil._vendor.atklogic import atkdl, atkproto
from hil.addresses import parse_address
from hil.artifacts import ArtifactSpool
from hil.drivers._atklogic import AtkCore, AtkLogicBackend
from hil.drivers.logic import LogicInstrument, logic_driver
from hil.errors import HilError


# -- fake device (deterministic samples through the real vendored code) -----
class FakeStream:
    def __init__(self, samples_by_ch: dict[int, list[int]], depth: int) -> None:
        self._b = {ch: atkdl.pack_samples(list(s))[: depth // 8]
                   for ch, s in samples_by_ch.items()}
        self.chan = {ch: bytearray(b) for ch, b in self._b.items()}
        self.orders: dict = {}
        self.trig_offset = 0
        self.rle_pairs = None
        self.raw_bytes = sum(len(b) for b in self._b.values())

    def channel_bytes(self, ch: int, depth: int) -> bytes:
        return self._b.get(ch, b"")[: depth // 8]

    def samples_available(self, ch: int) -> int:
        return len(self._b.get(ch, b"")) * 8


class FakeUsb:
    manufacturer = "ATK"
    product = "ATK-Logic-Analyzer"
    serial_number = "FAKE01"


class FakeDevice:
    def __init__(self, samples_by_ch: dict[int, list[int]]) -> None:
        self._s = samples_by_ch
        self.stopped = self.closed = False
        self.last: dict | None = None

    def call(self, fn, retries=None):
        return fn(FakeUsb())

    def acquire(self, rate_mhz, depth, threshold_v=1.5, trigger="rising", channel=0,
                mode="buffer", timeout_s=None, extra_channels=(), rle=False, progress=True):
        want = [channel] + [c for c in extra_channels if c != channel]
        self.last = {"rate_mhz": rate_mhz, "depth": depth, "channel": channel,
                     "extra": list(extra_channels), "trigger": trigger, "mode": mode}
        sbc = {c: self._s.get(c, [0] * depth) for c in want}
        return {"stream": FakeStream(sbc, depth), "complete": True, "elapsed_s": 0.01,
                "raw_need_bytes": depth // 8}

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _core(samples_by_ch: dict[int, list[int]], **kw) -> AtkCore:
    """A real AtkCore over a fake device; ``_backend_done`` skips the pyusb import."""
    c = AtkCore(dev=FakeDevice(samples_by_ch), **kw)
    c._backend_done = True
    return c


def _uart_waveform(data: bytes, srate: int = 1_000_000, baud: int = 100_000,
                   idle: int = 200) -> list[int]:
    spb = int(round(srate / baud))                 # samples per bit
    out = [1] * idle                               # idle high
    for byte in data:
        out += [0] * spb                           # start bit
        for b in range(8):                         # data bits, LSB first
            out += [(byte >> b) & 1] * spb
        out += [1] * spb                           # stop bit
    return out + [1] * idle


# -- core: identity + capture normalization --------------------------------
def test_core_identify_offline():
    ident = _core({0: [0, 1] * 64}).identify()
    assert ident["usb_id"] == "1A86:FFCC"
    assert ident["serial"] == "FAKE01" and ident["channels"] == 16
    assert ident["model"] == "ATK-Logic-Analyzer"
    assert ident["max_sample_rate_hz"] == max(atkproto.RATES) * 1_000_000


def test_capture_snaps_rate_and_rounds_depth():
    c = _core({0: [1] * 8})
    cap = c.capture(channels=[0], depth=1001, sample_rate_hz=3_000_000)
    assert cap.rate_mhz in atkproto.RATES            # 3 MHz snapped to a real gear
    assert cap.depth == 1000                          # rounded down to a multiple of 8
    assert c._dev.last["depth"] == 1000


def test_capture_channels_dedupe_and_primary():
    c = _core({0: [0] * 8, 3: [1] * 8})
    cap = c.capture(channels=[3, 0, 3], depth=64, sample_rate_hz=1e6)
    assert cap.chs == [3, 0]
    assert c._dev.last["channel"] == 3 and c._dev.last["extra"] == [0]


def test_capture_rejects_bad_channel():
    with pytest.raises(HilError):
        _core({0: [0] * 8}).capture(channels=[99], depth=64, sample_rate_hz=1e6)


# -- core: save / load round-trips -----------------------------------------
def test_save_load_atkdl_roundtrip(tmp_path):
    n = 512
    pat = [(i // 8) % 2 for i in range(n)]
    c = _core({0: pat})
    cap = c.capture(channels=[0], depth=n, sample_rate_hz=1e6)
    p = tmp_path / "cap.atkdl"
    c.save_capture(cap, "atkdl", p)
    assert p.exists() and p.stat().st_size > 0
    assert p.read_bytes()[:2] == b"PK"                # .atkdl is a zip container
    samples, srate, info = c.load_samples(p, channels=[0])
    assert srate == 1_000_000 and info["format"] == "atkdl"
    assert len(samples[0]) == n and list(samples[0]) == pat


def test_save_load_csv_roundtrip(tmp_path):
    n = 64
    pat0 = [i % 2 for i in range(n)]
    pat1 = [(i + 1) % 2 for i in range(n)]
    c = _core({0: pat0, 1: pat1})
    cap = c.capture(channels=[0, 1], depth=n, sample_rate_hz=1e6)
    p = tmp_path / "cap.csv"
    c.save_capture(cap, "csv", p)
    assert p.read_text(encoding="utf-8").splitlines()[0] == "index,time_s,ch0,ch1"
    samples, srate, info = c.load_samples(p, channels=[0, 1])
    assert info["format"] == "csv" and abs(srate - 1e6) < 1e3
    assert list(samples[0]) == pat0 and list(samples[1]) == pat1


def test_save_raw_is_packed_primary_channel(tmp_path):
    n = 128
    c = _core({0: [1, 0] * (n // 2)})
    cap = c.capture(channels=[0], depth=n, sample_rate_hz=1e6)
    p = tmp_path / "cap.bin"
    c.save_capture(cap, "raw", p)
    assert p.stat().st_size == n // 8                 # 8 samples per byte


def test_load_missing_artifact_errors(tmp_path):
    with pytest.raises(HilError):
        _core({}).load_samples(tmp_path / "nope.atkdl")


# -- backend adaptation (LogicBackend protocol) ----------------------------
def _backend(tmp_path, samples_by_ch, **corekw) -> AtkLogicBackend:
    return AtkLogicBackend(spool=ArtifactSpool(tmp_path), timeout=5.0,
                           core=_core(samples_by_ch, **corekw))


def test_backend_flow_offline(tmp_path):
    n = 256
    pat = [(i // 16) % 2 for i in range(n)]
    be = _backend(tmp_path, {0: pat, 1: [0] * n})
    be.open()                                          # no-op: backend already set up
    h = be.hello()
    assert h["device"]["model"] == "ATK-Logic-Analyzer" and h["device"]["serial"] == "FAKE01"
    assert h["channels"] == 16 and h["usb_id"] == "1A86:FFCC"
    assert isinstance(h["decoders"], list)
    be.configure({"channels": [0, 1], "sample_rate_hz": 1e6, "depth_samples": n})
    cid = be.arm()
    assert be.status(cid)["state"] == "done"
    assert be.wait(cid, 1.0)["state"] == "done"
    art = be.fetch(cid, fmt="atkdl")
    assert art["kind"] == "logic" and art["format"] == "atkdl"
    assert art["sample_count"] == n and art["channels"] == [0, 1]
    # HIL's default srzip is served as a real .atkdl container
    art2 = be.fetch(cid, fmt="srzip")
    assert art2["format"] == "atkdl" and open(art2["path"], "rb").read(2) == b"PK"
    be.stop()
    be.close()


def test_backend_fetch_without_arm_errors(tmp_path):
    be = _backend(tmp_path, {0: [0] * 8})
    be.open()
    with pytest.raises(HilError):
        be.fetch("", fmt="atkdl")


def test_factory_usb_builds_inprocess_backend(monkeypatch):
    monkeypatch.setattr(AtkCore, "setup_backend", lambda self: None)
    inst = logic_driver(parse_address("logic://usb"), None, 5.0)
    assert isinstance(inst, LogicInstrument) and isinstance(inst.backend, AtkLogicBackend)
    assert inst.backend.info() == "atk-logic@usb"
    inst.close()


def test_factory_usb_passes_serial_param(monkeypatch):
    monkeypatch.setattr(AtkCore, "setup_backend", lambda self: None)
    inst = logic_driver(parse_address("logic://usb?serial=ATK22"), None, 5.0)
    assert inst.backend.core.serial == "ATK22"
    assert inst.backend.info() == "atk-logic@usb:ATK22"
    inst.close()


# -- decode (needs the vendor sigrok PDs; skipped when absent) --------------
def _require_uart(core: AtkCore) -> None:
    try:
        core.srdhost.resolve("uart", core.decoders_dir)
    except Exception:
        pytest.skip(f"no 'uart' sigrok decoder in {core.decoders_dir}")


def test_core_decode_uart_offline(tmp_path):
    wave = _uart_waveform(b"HELLO")
    depth = (len(wave) + 7) // 8 * 8
    wave = wave + [1] * (depth - len(wave))
    core = _core({0: wave})
    _require_uart(core)
    cap = core.capture(channels=[0], depth=depth, sample_rate_hz=1e6)
    p = tmp_path / "uart.atkdl"
    core.save_capture(cap, "atkdl", p)
    res = core.decode(p, [{"name": "uart", "channels": {"rx": 0},
                           "options": {"baudrate": 100000}}])
    assert "uart" in res["chain"][0]
    assert res["frames"] >= 1
    out, frames = core.write_decode(res, "csv", tmp_path / "uart.csv")
    assert frames >= 1 and out.read_text(encoding="utf-8").startswith("sample,t_ms,protocol")


def test_backend_decode_artifact_offline(tmp_path):
    wave = _uart_waveform(b"\x55\xAA")
    depth = (len(wave) + 7) // 8 * 8
    wave = wave + [1] * (depth - len(wave))
    be = _backend(tmp_path, {0: wave})
    _require_uart(be.core)
    be.configure({"channels": [0], "sample_rate_hz": 1e6, "depth_samples": depth})
    cid = be.arm()
    art = be.fetch(cid, fmt="atkdl")
    dec = be.decode(art["path"], [{"name": "uart", "channels": {"rx": 0},
                                   "options": {"baudrate": 100000}}], fmt="csv")
    assert dec["kind"] == "decoded" and dec["format"] == "csv"
    assert dec["frames"] >= 1 and dec["decoders"] == ["uart"]


def test_decode_without_config_or_name_errors(tmp_path):
    core = _core({0: [0] * 64})
    cap = core.capture(channels=[0], depth=64, sample_rate_hz=1e6)
    p = tmp_path / "c.atkdl"
    core.save_capture(cap, "atkdl", p)
    with pytest.raises(HilError):                 # no decoders + no official config in file
        core.decode(p, [])
