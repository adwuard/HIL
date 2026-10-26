"""Offline tests for the ``hil atklogic`` CLI group — no hardware, no pyusb.

A fake device is injected by patching :meth:`AtkCore._device`, so ``info`` /
``capture`` drive the *real* vendored ``atkproto``/``atkdl`` code paths through the
CLI (argparse → dispatch → ``_emit``) with deterministic samples. ``list`` /
``decode`` / ``convert`` run purely offline on temp artifacts and a fake decoder
dir. The UART-decode test is skipped when no sigrok ``uart`` PD is present.
"""

from __future__ import annotations

import json

import pytest

import hil.drivers._atklogic as A
from hil._vendor.atklogic import atkdl
from hil.cli import main


# -- fake device (deterministic samples through the real vendored code) -----
class FakeStream:
    def __init__(self, samples_by_ch: dict[int, list[int]], depth: int) -> None:
        self._b = {ch: atkdl.pack_samples(list(s))[: depth // 8]
                   for ch, s in samples_by_ch.items()}
        self.trig_offset = 0

    def channel_bytes(self, ch: int, depth: int) -> bytes:
        return self._b.get(ch, b"")[: depth // 8]


class FakeUsb:
    manufacturer = "ATK"
    product = "ATK-Logic-Analyzer"
    serial_number = "FAKE01"


class FakeDevice:
    def __init__(self, samples_by_ch: dict[int, list[int]]) -> None:
        self._s = samples_by_ch

    def call(self, fn, retries=None):
        return fn(FakeUsb())

    def acquire(self, rate_mhz, depth, threshold_v=1.5, trigger="rising", channel=0,
                mode="buffer", timeout_s=None, extra_channels=(), rle=False, progress=True):
        want = [channel] + [c for c in extra_channels if c != channel]
        sbc = {c: self._s.get(c, [0] * depth) for c in want}
        return {"stream": FakeStream(sbc, depth), "complete": True, "elapsed_s": 0.01,
                "raw_need_bytes": depth // 8}

    def stop(self):
        pass

    def close(self):
        pass


def _patch_device(monkeypatch, samples_by_ch: dict[int, list[int]]) -> FakeDevice:
    """Make every ``AtkCore`` the CLI builds drive a fake device (no pyusb)."""
    fake = FakeDevice(samples_by_ch)
    monkeypatch.setattr(A.AtkCore, "setup_backend", lambda self: None)
    monkeypatch.setattr(A.AtkCore, "_device", lambda self: fake)
    return fake


def _fake_decoders(tmp_path, names=("uart", "i2c")):
    """A decoder dir ``list_decoders`` accepts (each name has a ``pd.py``)."""
    dd = tmp_path / "decoders"
    for n in names:
        (dd / n).mkdir(parents=True, exist_ok=True)
        (dd / n / "pd.py").write_text("# stub\n", encoding="utf-8")
    return dd


def _uart_waveform(data: bytes, srate: int = 1_000_000, baud: int = 100_000,
                   idle: int = 200) -> list[int]:
    spb = int(round(srate / baud))
    out = [1] * idle
    for byte in data:
        out += [0] * spb
        for b in range(8):
            out += [(byte >> b) & 1] * spb
        out += [1] * spb
    return out + [1] * idle


# -- list (offline; fake decoder dir) ---------------------------------------
def test_atklogic_list_cli(tmp_path, capsys):
    dd = _fake_decoders(tmp_path)
    rc = main(["atklogic", "list", "--decoders", str(dd), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert payload["count"] == 2 and payload["protocols"] == ["i2c", "uart"]

    rc = main(["atklogic", "list", "i2c", "--decoders", str(dd), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["protocols"] == ["i2c"] and payload["filter"] == "i2c"


def test_atklogic_list_missing_dir_is_empty(tmp_path, capsys):
    rc = main(["atklogic", "list", "--decoders", str(tmp_path / "nope"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["count"] == 0 and payload["protocols"] == []


# -- info / capture (fake device through the real vendored code) ------------
def test_atklogic_info_cli(monkeypatch, capsys):
    _patch_device(monkeypatch, {0: [0, 1] * 64})
    rc = main(["atklogic", "info", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert payload["usb_id"] == "1A86:FFCC" and payload["serial"] == "FAKE01"
    assert payload["channels"] == 16 and payload["model"] == "ATK-Logic-Analyzer"


def test_atklogic_capture_cli_atkdl(tmp_path, monkeypatch, capsys):
    n = 256
    _patch_device(monkeypatch, {0: [(i // 8) % 2 for i in range(n)]})
    out = tmp_path / "cap.atkdl"
    rc = main(["atklogic", "capture", "--channels", "0", "--rate", "1e6",
               "--depth", str(n), "--fmt", "atkdl", "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert payload["format"] == "atkdl" and payload["sample_count"] == n
    assert payload["channels"] == [0] and out.read_bytes()[:2] == b"PK"


def test_atklogic_capture_cli_quiet_channel_no_crash(tmp_path, monkeypatch, capsys):
    # Regression: waveform_stats yields frequency_hz=None for a flat channel; the
    # human report must guard it (was a TypeError on f"{None:.6g}").
    n = 256
    _patch_device(monkeypatch, {0: [(i // 8) % 2 for i in range(n)], 1: [0] * n})
    out = tmp_path / "cap.csv"
    rc = main(["atklogic", "capture", "--channels", "0,1", "--rate", "1e6",
               "--depth", str(n), "--fmt", "csv", "--out", str(out)])
    txt = capsys.readouterr().out
    assert rc == 0 and "freq=0Hz" in txt and out.exists()


# -- convert (offline; re-serialize a saved artifact) -----------------------
def test_atklogic_convert_csv_to_raw(tmp_path, capsys):
    src = tmp_path / "in.csv"
    src.write_text("index,time_s,ch0,ch1\n"
                   + "".join(f"{i},{i/1e6:.9g},{i%2},{1-(i%2)}\n" for i in range(16)),
                   encoding="utf-8")
    out = tmp_path / "out.bin"
    rc = main(["atklogic", "convert", str(src), "--fmt", "raw", "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True and payload["format"] == "raw"
    assert payload["sample_count"] == 16 and payload["bytes"] == 2 and out.exists()


def test_atklogic_convert_channel_subset(tmp_path, capsys):
    src = tmp_path / "in.csv"
    src.write_text("index,time_s,ch0,ch1\n"
                   + "".join(f"{i},{i/1e6:.9g},{i%2},{1-(i%2)}\n" for i in range(8)),
                   encoding="utf-8")
    out = tmp_path / "out.csv"
    rc = main(["atklogic", "convert", str(src), "--channels", "1", "--fmt", "csv",
               "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["channels"] == [1]
    assert out.read_text(encoding="utf-8").splitlines()[0] == "index,time_s,ch1"


# -- decode -----------------------------------------------------------------
def test_atklogic_decode_no_config_message(tmp_path, monkeypatch, capsys):
    # A flat capture has no embedded decoder config; decode with no --decoder must
    # fail cleanly (JSON error) and point the user at --decoder.
    _patch_device(monkeypatch, {0: [0] * 256})
    cap = tmp_path / "flat.atkdl"
    main(["atklogic", "capture", "--channels", "0", "--rate", "1e6", "--depth", "256",
          "--fmt", "atkdl", "--out", str(cap), "--json"])
    capsys.readouterr()                                   # discard the capture output
    rc = main(["atklogic", "decode", str(cap), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["ok"] is False and "--decoder" in payload["error"]


def test_atklogic_decode_uart_cli(tmp_path, monkeypatch, capsys):
    probe = A.AtkCore()
    try:
        probe.srdhost.resolve("uart", probe.decoders_dir)
    except Exception:
        pytest.skip(f"no 'uart' sigrok decoder in {probe.decoders_dir}")
    wave = _uart_waveform(b"HELLO")
    depth = (len(wave) + 7) // 8 * 8
    wave = wave + [1] * (depth - len(wave))
    _patch_device(monkeypatch, {0: wave})
    cap = tmp_path / "uart.atkdl"
    main(["atklogic", "capture", "--channels", "0", "--rate", "1e6", "--depth", str(depth),
          "--fmt", "atkdl", "--out", str(cap), "--json"])
    capsys.readouterr()                                   # discard the capture output
    out = tmp_path / "uart.csv"
    rc = main(["atklogic", "decode", str(cap), "--decoder", "uart", "--map", "rx=0",
               "--option", "baudrate=100000", "--fmt", "csv", "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True and payload["frames"] >= 1
    assert "uart" in payload["chain"][0] and out.exists()


# -- standalone shim delegates to the same parser ---------------------------
def test_atklogic_standalone_shim(tmp_path, capsys):
    dd = _fake_decoders(tmp_path)
    rc = A.main(["list", "--decoders", str(dd), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["count"] == 2
