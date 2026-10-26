"""Direct tests for the vendored ``atk-logic-mcp`` core (``hil._vendor.atklogic``).

These lock the two things HIL changed when vendoring (b1) and the portability
patches (b2), at the vendored layer itself — independent of ``AtkCore``/the backend
(which ``test_drivers_atklogic.py`` covers) and the CLI (``test_cli_atklogic.py``):

* packaging: the flat intra-imports were made relative (``atkdl.P is atkproto``);
* portability: ``_default_decoders_dir()`` honours ``$ATK_DECODERS_DIR`` (was a
  hardcoded Windows path) and ``setup_backend()`` discovers libusb cross-platform
  without raising;
* the core primitives the wrapper relies on (bit packing, channel indexing, device
  constants, the ``.atkdl`` container round-trip).

All offline; ``pyusb``/``libusb`` are optional and the one test that needs pyusb is
skipped when it is absent.
"""

from __future__ import annotations

import os

import pytest

from hil._vendor.atklogic import atkdl, atkproto, srdhost


# -- packaging (b1: relative imports) ---------------------------------------
def test_package_imports_and_wiring():
    from hil._vendor import atklogic

    assert atkdl.P is atkproto                 # was a flat `import atkproto as P`
    assert set(atklogic.__all__) == {"atkproto", "atkdl", "srdhost"}


def test_device_constants():
    assert atkproto.VID == 0x1A86 and atkproto.PID == 0xFFCC
    assert atkproto.CHANNELS == 16
    assert max(atkproto.RATES) == 200 and 1 in atkproto.RATES


# -- core primitives ---------------------------------------------------------
def test_pack_bits_roundtrip():
    pat = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 1]   # 16 samples (mult of 8)
    assert list(atkproto.bits_from_bytes(atkdl.pack_samples(pat))) == pat


def test_channel_to_index():
    assert atkproto.channel_to_index("CH3") == 3
    assert atkproto.channel_to_index("ch7") == 7
    assert atkproto.channel_to_index(5) == 5
    assert atkproto.channel_to_index("0") == 0


class _MiniStream:
    """The only thing ``stream_to_atkdl`` needs is ``channel_bytes``."""

    def __init__(self, samples_by_ch: dict[int, list[int]], depth: int) -> None:
        self._b = {ch: atkdl.pack_samples(list(s))[: depth // 8]
                   for ch, s in samples_by_ch.items()}

    def channel_bytes(self, ch: int, depth: int) -> bytes:
        return self._b.get(ch, b"")[: depth // 8]


def test_atkdl_container_roundtrip(tmp_path):
    n = 256
    pat = [(i // 8) % 2 for i in range(n)]
    p = tmp_path / "c.atkdl"
    atkdl.stream_to_atkdl(_MiniStream({0: pat}, n), [0], 1, n, str(p))   # 1 MHz
    assert p.read_bytes()[:2] == b"PK"                     # .atkdl is a zip container
    s = atkdl.read_atkdl(str(p))
    assert float(s.sample_rate_hz) == 1_000_000
    assert list(s.window(0, 0, n)) == pat


# -- portability (b2) --------------------------------------------------------
def test_default_decoders_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ATK_DECODERS_DIR", str(tmp_path))
    assert srdhost._default_decoders_dir() == str(tmp_path)
    monkeypatch.delenv("ATK_DECODERS_DIR", raising=False)
    assert isinstance(srdhost._default_decoders_dir(), str)   # a platform fallback


def test_list_decoders_enumerates_pds(tmp_path):
    for n in ("uart", "i2c"):
        (tmp_path / n).mkdir()
        (tmp_path / n / "pd.py").write_text("# stub\n", encoding="utf-8")
    assert srdhost.list_decoders(str(tmp_path)) == ["i2c", "uart"]   # sorted


def test_list_decoders_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        srdhost.list_decoders(str(tmp_path / "nope"))


def test_setup_backend_does_not_raise():
    pytest.importorskip("usb")                    # pyusb is optional
    found = atkproto.setup_backend()
    assert found is None or os.path.exists(found)  # a real libusb path, or a clean None
