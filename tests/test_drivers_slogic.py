"""Tests for Sipeed SLogic Combo 8 support — the sigrok-cli subprocess backend.

Everything runs hardware-free and with no ``sigrok-cli`` installed: the
:class:`~hil.drivers._sigrok.SigrokBackend` argv builders are pure, and a
``runner`` injection seam lets the whole scan -> capture -> fetch -> decode flow
be driven by a fake subprocess (mirroring how the daemon/sim/atk logic backends
are tested). Routing, addressing, the device profile, the provisioning recipe and
discovery are covered too — including the invariant that ``driver: logic`` still
resolves to the license-clean ATK-Logic recipe (the SLogic recipe is driverless).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hil import discovery, resolve_target
from hil.addresses import parse_address
from hil.artifacts import ArtifactSpool, metadata_path
from hil.drivers import _sigrok
from hil.drivers._sigrok import (
    SigrokBackend,
    _channels_arg,
    _decoder_spec,
    _driver_spec,
    _rate_arg,
    _time_arg,
    _trigger_arg,
    parse_scan,
    scan_devices,
)
from hil.drivers.logic import LogicInstrument, logic_driver
from hil.errors import HilError
from hil.provision import catalog
from hil.registry import load_registry

# A realistic `sigrok-cli --scan` transcript: a header line to skip, the device
# line (driver[:conn] - name [S/N: ..] with N channels), and per-channel rows.
_SCAN_OUTPUT = """The following devices were found:
    sipeed-slogic-analyzer:conn=359f.0300 - SIPEED USB TO LA [S/N: SL000123] with 8 channels:
        D0: D0
        D1: D1
"""

# The exact transcript a real SLogic Combo 8 produces (verified against
# sigrok-cli 0.8.0 + Sipeed's slogic-dev fork): NO ``:conn=`` token, model
# "(old) SLogic8 U2", and a global ``--scan`` that also lists libsigrok's
# built-in ``demo`` pseudo-device (which scan_devices must filter out).
_SCAN_OUTPUT_REAL = """The following devices were found:
demo - Demo device with 13 channels: D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4
sipeed-slogic-analyzer - (old) SLogic8 U2 [S/N: SI 8ch] with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7
"""


class _FakeSigrok:
    """A fake ``sigrok-cli`` subprocess: ``(argv, timeout) -> result dict``.

    Materializes the capture (``-o``) and convert (``--output-file``) outputs so
    ``arm()``/``fetch()`` find them on disk, returns canned ``--scan`` text, and
    emits decode annotations on stdout. ``rc`` forces a non-zero exit.
    """

    def __init__(self, *, scan: str = "", decode_text: str = "", rc: int = 0) -> None:
        self.scan = scan
        self.decode_text = decode_text
        self.rc = rc
        self.calls: list[list[str]] = []

    def _res(self, text: str = "") -> dict:
        return {"rc": self.rc, "stdout": text, "stderr": "", "output": text, "timeout": False}

    def __call__(self, argv: list[str], timeout: float) -> dict:
        self.calls.append(list(argv))
        if "--scan" in argv:
            return self._res(self.scan)
        if "--protocol-decoders" in argv:
            return self._res(self.decode_text)
        for flag in ("-o", "--output-file"):
            if flag in argv:
                out = Path(argv[argv.index(flag) + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.suffix == ".csv":
                    out.write_text("index,time_s,ch0,ch1\n0,0,1,0\n1,1e-6,0,1\n",
                                   encoding="utf-8")
                else:
                    out.write_bytes(b"PK\x03\x04fake-srzip-payload")
        return self._res("")


# -- pure helpers -----------------------------------------------------------
def test_pure_arg_helpers():
    assert _driver_spec("drv", "") == "drv"
    assert _driver_spec("drv", "359f.0300") == "drv:conn=359f.0300"
    assert _rate_arg(1e6) == "1000000"
    assert _time_arg(2.0) == "2s" and _time_arg(0.5) == "0.5s"
    assert _channels_arg(4) == "D0,D1,D2,D3"                  # indices -> sigrok names
    assert _channels_arg([1, 3]) == "D1,D3"
    assert _channels_arg([0, 1], ["A", "B", "C"]) == "A,B"    # resolved vs real names
    assert _channels_arg(8, [f"D{i}" for i in range(8)]) == ""  # all channels -> omit
    assert _channels_arg(None) == ""
    assert _trigger_arg("rising") == "0=r"                    # no names -> index fallback
    assert _trigger_arg({"channel": 2, "edge": "falling"}, ["D0", "D1", "D2"]) == "D2=f"
    assert _trigger_arg({"channel": 2, "edge": "falling"}) == "2=f"
    assert _trigger_arg({"channel": 0, "level": 1}) == "0=1"
    assert _trigger_arg(None) == ""
    assert _decoder_spec("i2c") == "i2c"
    assert _decoder_spec({"name": "i2c", "channels": {"scl": 0, "sda": 1}}) == "i2c:scl=D0:sda=D1"
    assert _decoder_spec({"name": "i2c", "channels": {"scl": 6, "sda": 7}},
                         [f"D{i}" for i in range(8)]) == "i2c:scl=D6:sda=D7"
    assert _decoder_spec({"name": "i2c", "channels": {"scl": "D6"}}) == "i2c:scl=D6"
    assert _decoder_spec({"name": "uart", "options": {"baudrate": 115200}}) == "uart:baudrate=115200"


def test_parse_scan_extracts_device_and_skips_noise():
    recs = parse_scan(_SCAN_OUTPUT)
    assert len(recs) == 1
    r = recs[0]
    assert r["driver"] == "sipeed-slogic-analyzer"
    assert r["conn"] == "359f.0300"
    assert r["model"] == "SIPEED USB TO LA"
    assert r["serial"] == "SL000123"
    assert r["channels"] == 8
    assert parse_scan("") == []
    assert parse_scan("The following devices were found:") == []


# -- argv builders ----------------------------------------------------------
def test_capture_argv_builder():
    be = SigrokBackend(driver="sipeed-slogic-analyzer", conn="359f.0300")
    argv = be.capture_argv("/tmp/out.srzip", sample_rate_hz=2_000_000, channels=[0, 1, 2],
                           samples=500, trigger={"channel": 1, "edge": "rising"})
    assert argv[0] == "sigrok-cli"
    assert "sipeed-slogic-analyzer:conn=359f.0300" in argv
    assert "samplerate=2000000" in argv and "D0,D1,D2" in argv and "500" in argv
    assert "D1=r" in argv
    assert argv[-4:] == ["--output-format", "srzip", "-o", "/tmp/out.srzip"]
    # selecting every channel omits --channels (sigrok's default = all channels)
    assert "--channels" not in be.capture_argv("/tmp/o.srzip", channels=list(range(8)))


def test_scan_convert_decode_argv_builders():
    be = SigrokBackend()
    assert be.scan_argv() == ["sigrok-cli", "--driver", "sipeed-slogic-analyzer", "--scan"]
    timed = be.capture_argv("/tmp/o.srzip", duration_s=2.0)
    assert "--time" in timed and "2s" in timed and "--samples" not in timed
    assert be.convert_argv("/tmp/in.srzip", "/tmp/out.csv", "csv") == [
        "sigrok-cli", "--input-file", "/tmp/in.srzip", "--output-format", "csv",
        "--output-file", "/tmp/out.csv"]
    dec = be.decode_argv("/tmp/in.srzip", [{"name": "uart", "channels": {"rx": 2, "tx": 3}}])
    assert "--protocol-decoders" in dec and "uart:rx=D2:tx=D3" in dec


def test_capture_params_clamp_floor_and_duration_uses_time(tmp_path):
    be = SigrokBackend(spool=ArtifactSpool(tmp_path), runner=_FakeSigrok())
    assert be.min_samples == 65536                      # slogic driver -> device floor
    assert SigrokBackend(driver="fx2lafw", runner=_FakeSigrok()).min_samples == 0
    # a small depth request is clamped up to the two-block floor (else the endpoint stalls)
    be.configure({"depth_samples": 1000, "sample_rate_hz": 1_000_000})
    p = be._capture_params()
    assert p["samples"] == 65536 and p["duration_s"] == 0.0
    assert "65536" in be.capture_argv("/tmp/o.srzip", samples=p["samples"],
                                      duration_s=p["duration_s"])
    # a duration request streams via --time (samples=0), which the device honors at any length
    be.configure({"duration_s": 0.02, "sample_rate_hz": 1_000_000})
    p2 = be._capture_params()
    assert p2["samples"] == 0 and p2["duration_s"] == 0.02
    argv = be.capture_argv("/tmp/o.srzip", samples=p2["samples"], duration_s=p2["duration_s"])
    assert "--time" in argv and "0.02s" in argv and "--samples" not in argv


def test_scan_devices_global_vs_driver_scoped():
    scoped = _FakeSigrok(scan=_SCAN_OUTPUT)
    recs = scan_devices(driver="sipeed-slogic-analyzer", runner=scoped)
    assert recs and recs[0]["driver"] == "sipeed-slogic-analyzer"
    assert scoped.calls[0][:3] == ["sigrok-cli", "--driver", "sipeed-slogic-analyzer"]
    glob = _FakeSigrok(scan=_SCAN_OUTPUT)
    scan_devices(runner=glob)                       # no driver -> a global --scan
    assert glob.calls[0] == ["sigrok-cli", "--scan"]


def test_parse_scan_handles_real_no_conn_format():
    # parse_scan is a faithful parser: it returns BOTH the demo line and the
    # real device, and copes with the conn-less transcript the SLogic emits.
    recs = parse_scan(_SCAN_OUTPUT_REAL)
    assert [r["driver"] for r in recs] == ["demo", "sipeed-slogic-analyzer"]
    slogic = recs[1]
    assert slogic["conn"] == ""                     # real output carries no :conn=
    assert slogic["model"] == "(old) SLogic8 U2"
    assert slogic["serial"] == "SI 8ch"
    assert slogic["channels"] == 8
    assert slogic["channel_names"] == ["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"]


def test_scan_devices_filters_demo_pseudo_device():
    glob = _FakeSigrok(scan=_SCAN_OUTPUT_REAL)
    recs = scan_devices(runner=glob)                # global --scan
    assert [r["driver"] for r in recs] == ["sipeed-slogic-analyzer"]   # demo dropped
    assert recs[0]["model"] == "(old) SLogic8 U2" and recs[0]["channels"] == 8


# -- identity ---------------------------------------------------------------
def test_hello_enriches_from_live_scan(tmp_path):
    be = SigrokBackend(spool=ArtifactSpool(tmp_path), runner=_FakeSigrok(scan=_SCAN_OUTPUT))
    h = be.hello()
    assert h["device"]["vendor"] == "Sipeed"
    assert h["device"]["model"] == "SIPEED USB TO LA"      # from the scan
    assert h["device"]["serial"] == "SL000123"
    assert h["channels"] == 8 and h["usb_id"] == "359f.0300"
    assert h["max_sample_rate_hz"] == 80_000_000 and "i2c" in h["decoders"]


def test_hello_static_when_scan_finds_nothing(tmp_path):
    be = SigrokBackend(spool=ArtifactSpool(tmp_path), runner=_FakeSigrok(scan=""))
    h = be.hello()
    assert h["device"]["model"] == "SLogic Combo 8"        # static fallback
    assert h["channels"] == 8 and h["usb_id"] == "359F:0300"


# -- capture / fetch / decode flow -----------------------------------------
def test_run_capture_and_decode_via_facade(tmp_path):
    spool = ArtifactSpool(tmp_path)
    fake = _FakeSigrok(decode_text="i2c\t0\tSTART\ni2c\t1\tADDR\ni2c\t2\tSTOP\n")
    inst = LogicInstrument(SigrokBackend(spool=spool, runner=fake), device=None, spool=spool)
    art = inst.run_capture(sample_rate_hz=1_000_000, depth_samples=1000, fmt="srzip")
    assert art.kind == "logic" and art.format == "srzip"
    assert Path(art.path).exists() and Path(art.path).suffix == ".srzip"
    assert art.sample_rate_hz == 1_000_000 and art.channels == list(range(8))
    assert metadata_path(art.path).exists()                # sidecar recorded by _finalize

    dec = inst.decode({"name": "i2c", "channels": {"scl": 0, "sda": 1}}, artifact=art)
    assert dec.kind == "decoded" and dec.frames == 3 and dec.parent == art.id
    assert "i2c" in dec.decoders

    cap_argv = " ".join(fake.calls[0])
    assert "--driver sipeed-slogic-analyzer" in cap_argv
    assert "--output-format srzip" in cap_argv and "--config samplerate=1000000" in cap_argv
    assert "--protocol-decoders i2c:scl=D0:sda=D1" in " ".join(fake.calls[-1])
    inst.close()


def test_fetch_serves_srzip_and_converts_csv(tmp_path):
    spool = ArtifactSpool(tmp_path)
    be = SigrokBackend(spool=spool, runner=_FakeSigrok())
    be.configure({"sample_rate_hz": 1e6, "depth_samples": 100})
    cid = be.arm()
    sr = be.fetch(cid, fmt="srzip")
    assert sr["format"] == "srzip" and Path(sr["path"]).suffix == ".srzip"
    csv_art = be.fetch(cid, fmt="csv")
    assert csv_art["format"] == "csv" and Path(csv_art["path"]).suffix == ".csv"
    assert Path(csv_art["path"]).exists()
    with pytest.raises(HilError):
        be.fetch(cid, fmt="vcd")                           # unsupported


def test_arm_failure_raises_and_marks_error(tmp_path):
    be = SigrokBackend(spool=ArtifactSpool(tmp_path), runner=_FakeSigrok(rc=1))
    be.configure({"sample_rate_hz": 1e6, "depth_samples": 100})
    with pytest.raises(HilError):
        be.arm()
    assert be.status()["state"] == "error"


# -- lifecycle / tooling ----------------------------------------------------
def test_open_raises_actionable_when_sigrok_cli_missing(monkeypatch):
    monkeypatch.setattr(_sigrok.shutil, "which", lambda name: None)
    with pytest.raises(HilError) as ei:
        SigrokBackend().open()
    msg = str(ei.value)
    assert "sigrok-cli" in msg and "hil setup sipeed-slogic-combo8" in msg


# -- routing + addressing ---------------------------------------------------
def test_logic_driver_routes_sigrok_hosts(monkeypatch):
    monkeypatch.setattr(_sigrok.shutil, "which", lambda name: "/fake/sigrok-cli")
    for host in ("sigrok", "slogic", "sipeed"):
        inst = logic_driver(parse_address(f"logic://{host}"), None, 5.0)
        try:
            assert isinstance(inst.backend, SigrokBackend)
            assert inst.backend.driver == "sipeed-slogic-analyzer"
        finally:
            inst.close()


def test_logic_driver_usb_with_driver_param_routes_to_sigrok(monkeypatch):
    monkeypatch.setattr(_sigrok.shutil, "which", lambda name: "/fake/sigrok-cli")
    addr = parse_address("logic://usb?driver=sipeed-slogic-analyzer&conn=359f.0300&channels=8")
    inst = logic_driver(addr, None, 5.0)
    try:
        assert isinstance(inst.backend, SigrokBackend)
        assert inst.backend.conn == "359f.0300" and inst.backend.channels == 8
    finally:
        inst.close()


def test_sigrok_address_roundtrip_has_no_port():
    for raw in ("logic://sigrok", "logic://slogic", "logic://sipeed",
                "logic://sigrok?conn=359f.0300",
                "logic://sigrok?driver=sipeed-slogic-analyzer&channels=8"):
        assert str(parse_address(raw)) == raw


# -- device profile + slug resolution ---------------------------------------
def test_device_profile_and_slug_resolution():
    reg = load_registry()
    d = reg["sipeed-slogic-combo8"]
    assert d.driver == "logic" and d.category == "logic-analyzer"
    assert d.default_address == "logic://sigrok"
    assert {"logic-capture", "protocol-decode", "stream-to-disk"} <= set(d.capabilities)
    assert d.safety.limits["channels"] == "1..8"
    assert d.safety.limits["sample_rate"] == "1..80000000 Hz"
    addr, dev = resolve_target("sipeed-slogic-combo8", devices=reg)
    assert str(addr) == "logic://sigrok" and dev.slug == "sipeed-slogic-combo8"


# -- provisioning recipe ----------------------------------------------------
def test_recipe_is_driverless_gated_and_well_formed():
    r = catalog.load_recipe_file(catalog.BUILTIN_RECIPES / "sipeed-slogic-combo8.yaml")
    assert r.slug == "sipeed-slogic-combo8" and r.driver == ""   # no driver->recipe claim
    assert r.license.startswith("GPL-3.0")
    assert r.detect and r.verify                                 # every recipe is checkable
    deps = next(s for s in r.steps if s.id == "build-deps")
    build = next(s for s in r.steps if s.id == "build-sigrok-stack")
    assert deps.privileged                                       # system install -> gated
    assert build.heavy and build.optional                        # long build -> never implicit


def test_recipe_does_not_steal_the_logic_driver():
    recipes, reg = catalog.load_recipes(), load_registry()
    logic_driver_recipes = [x.slug for x in recipes.values() if x.driver == "logic"]
    assert logic_driver_recipes == ["alientek-atk-logic"]        # unchanged by the SLogic recipe
    assert [x.slug for x in catalog.resolve_targets(["logic"], recipes=recipes, registry=reg)] \
        == ["alientek-atk-logic"]
    assert [x.slug for x in catalog.resolve_targets(["sipeed-slogic-combo8"],
                                                    recipes=recipes, registry=reg)] \
        == ["sipeed-slogic-combo8"]
    assert catalog.recipe_for(device_slug="sipeed-slogic-combo8", recipes=recipes).slug \
        == "sipeed-slogic-combo8"


# -- discovery --------------------------------------------------------------
def test_backend_flags_include_sigrok():
    flags = discovery._backend_flags()
    assert "sigrok" in flags and isinstance(flags["sigrok"], bool)


def test_logic_analyzers_delegates_and_swallows_errors(monkeypatch):
    monkeypatch.setattr(_sigrok, "scan_devices",
                        lambda **kw: [{"driver": "sipeed-slogic-analyzer", "conn": "359f.0300",
                                       "model": "SIPEED USB TO LA", "serial": "SL1", "channels": 8}])
    assert discovery._logic_analyzers(3.0)[0]["driver"] == "sipeed-slogic-analyzer"

    def boom(**kw):
        raise RuntimeError("no sigrok-cli")

    monkeypatch.setattr(_sigrok, "scan_devices", boom)
    assert discovery._logic_analyzers(3.0) == []                 # best-effort: never raises


def test_discover_logic_yields_uniform_record(monkeypatch):
    monkeypatch.setattr(discovery, "_backend_flags",
                        lambda: {"visa": False, "serial": False, "zeroconf": False,
                                 "debug": False, "sigrok": True})
    monkeypatch.setattr(discovery, "_logic_analyzers",
                        lambda timeout: [{"driver": "sipeed-slogic-analyzer", "conn": "359f.0300",
                                          "model": "SIPEED USB TO LA", "serial": "SL1",
                                          "channels": 8}])
    res = discovery.discover(("logic",), identify=False)
    assert res["ok"] is True and res["count"] == 1
    dev = res["devices"][0]
    assert dev["interface"] == "logic" and dev["backend"] == "sigrok"
    assert dev["address"] == "logic://sigrok?conn=359f.0300"
    assert dev["matched_slug"] == "sipeed-slogic-combo8"


def test_discover_logic_is_silent_without_sigrok(monkeypatch):
    monkeypatch.setattr(discovery, "_backend_flags",
                        lambda: {"visa": False, "serial": False, "zeroconf": False,
                                 "debug": False, "sigrok": False})
    res = discovery.discover(("logic",), identify=False)
    assert res["ok"] is True and res["devices"] == []
