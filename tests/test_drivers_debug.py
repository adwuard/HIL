"""Tests for the flash/debug driver, its backends, and the debug:// factory.

Everything runs hardware-free: the simulator backend is an in-memory Cortex-M
target, and the real-tool backend is exercised through its *pure* argv builders
(:meth:`ToolBackend._command`) so no probe-rs/openocd/esptool/stlink/pyocd binary
needs to be installed — exactly the isolation the logic-driver tests use.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hil.addresses import parse_address
from hil.artifacts import ArtifactSpool
from hil.drivers import available_drivers, get_driver
from hil.drivers.debug import (
    DEBUG_ACTIONS,
    DebugInstrument,
    DebugSimBackend,
    ToolBackend,
    _as_int,
    _hex,
    _parse_hex_words,
    debug_driver,
    route_auto,
)
from hil.errors import HilError, SafetyViolationError
from hil.registry import load_registry


# -- helpers ----------------------------------------------------------------
def test_as_int_accepts_hex_dec_and_int():
    assert _as_int(42) == 42
    assert _as_int("0x2000") == 0x2000
    assert _as_int("4096") == 4096
    assert _as_int("0b1010") == 0b1010


def test_hex_formats_lowercase_no_padding():
    assert _hex(0x08000151) == "0x8000151"
    assert _hex(0x1000) == "0x1000"
    assert _hex(_as_int("0x0800_0000".replace("_", ""))) == "0x8000000"


def test_parse_hex_words_drops_addr_and_ascii_gutter():
    # OpenOCD mdw shape
    assert _parse_hex_words("0x08000000: 20002000 08000151") == [0x20002000, 0x08000151]
    # pyOCD read32 shape (ASCII gutter after '|' is dropped, leading addr: dropped)
    assert _parse_hex_words("08000000:  20002000 08000151  |.... ....|") == [0x20002000, 0x08000151]
    # esptool "addr = value" shape keeps both hex tokens, drops the '='
    assert _parse_hex_words("40000000 = 71") == [0x40000000, 0x71]


def test_route_auto_picks_tool_by_chip_family():
    assert route_auto("esp32") == "esptool"
    assert route_auto("", "esp32s3") == "esptool"
    assert route_auto("stm32f4x") == "stlink"
    assert route_auto("nrf52840") == "probe-rs"
    assert route_auto() == "probe-rs"            # unknown -> license-safe default


# -- simulator backend ------------------------------------------------------
def test_sim_backend_target_info_and_probe_list():
    be = DebugSimBackend()
    info = be.target_info()
    assert info["tool"] == "simulator" and info["chip"] == "sim-target"
    assert info["core"] == "cortex-m4" and info["state"] == "running"
    assert info["flashed"] is False
    probes = be.probe_list()
    assert len(probes) == 1 and probes[0]["transport"] == "swd"


def test_sim_backend_run_control_state_machine():
    be = DebugSimBackend()
    assert be.halt()["state"] == "halted"
    assert be.resume()["state"] == "running"
    pc0 = be.read_reg("pc")
    stepped = be.step()
    assert stepped["state"] == "halted" and be.read_reg("pc") == pc0 + 2
    rst = be.reset(halt=True)
    assert rst["state"] == "halted"
    assert be.read_reg("pc") == be.read_mem(0x4)[0] & ~1     # reset vector


def test_sim_backend_mem_read_write_and_tick_counter():
    be = DebugSimBackend()
    assert be.read_mem(0x40000000)[0] == 0x71               # WHO_AM_I
    be.write_mem(0x20000000, [0xDEADBEEF], 32)
    assert be.read_mem(0x20000000)[0] == 0xDEADBEEF
    t1 = be.read_mem(0x40000004)[0]                          # free-running tick
    t2 = be.read_mem(0x40000004)[0]
    assert t2 == t1 + 1


def test_sim_backend_reg_read_write_and_width_mask():
    be = DebugSimBackend()
    be.write_reg("r0", 0x1234)
    assert be.read_reg("r0") == 0x1234
    be.write_mem(0x20001000, [0x11223344], 8)               # 8-bit access masks
    assert be.read_mem(0x20001000, 8, 1)[0] == 0x44


def test_sim_backend_flash_records_fingerprint(tmp_path):
    fw = tmp_path / "app.bin"
    fw.write_bytes(b"\x00\x01\x02\x03firmware")
    be = DebugSimBackend()
    res = be.flash(str(fw), chip="nrf52840", address=0x0, verify=True)
    assert res["kind"] == "flash" and res["ok"] is True
    assert res["size_bytes"] == fw.stat().st_size and len(res["sha256"]) == 64
    assert be.target_info()["flashed"] is True


def test_sim_backend_erase_clears_flash_region():
    be = DebugSimBackend()
    be.write_mem(0x08000000, [0x1234], 32)
    res = be.erase()
    assert res["kind"] == "erase" and res["ok"] is True
    assert be.read_mem(0x08000000)[0] == 0                   # erased to 0
    assert be.target_info()["flashed"] is False


def test_sim_backend_rtt_gdb_and_wait_bootloader():
    be = DebugSimBackend()
    rtt = be.rtt(0.05)
    assert rtt["kind"] == "rtt" and len(rtt["lines"]) == 4
    assert "wifi_connected" in rtt["output"]
    gdb = be.gdb("dump.gdb")
    assert gdb["kind"] == "gdb" and gdb["exit_code"] == 0 and "Breakpoint" in gdb["output"]
    assert be.wait_bootloader()["state"] == "bootloader"


# -- instrument facade (on the simulator) -----------------------------------
def _instrument(tmp_path, device=None):
    spool = ArtifactSpool(tmp_path)
    return DebugInstrument(DebugSimBackend(), device=device, timeout=5.0, spool=spool)


def test_instrument_idn_and_queries(tmp_path):
    inst = _instrument(tmp_path)
    assert inst.idn() == "HIL,sim-target,SIM0001,sim-1.0.0"
    assert inst.query("*IDN?") == "HIL,sim-target,SIM0001,sim-1.0.0"
    assert inst.query(":PROBE?").startswith("1 probe(s):")
    assert "chip=sim-target" in inst.query(":TARG?")
    assert inst.query(":FW?") == "sim-1.0.0"
    assert inst.query("*OPC?") == "1"
    assert inst.query("SYST:ERR?") == '0,"No error"'


def test_instrument_state_is_live_after_control_ops(tmp_path):
    inst = _instrument(tmp_path)
    assert inst.query(":STAT?") == "running"
    inst.write(":HALT")
    assert inst.query(":STAT?") == "halted"
    inst.write(":STEP")
    assert inst.query(":STAT?") == "halted"
    inst.write(":RESUME")
    assert inst.query(":STAT?") == "running"
    inst.write(":RESET")
    assert inst.query(":STAT?") == "running"


def test_instrument_mem_and_reg_facade(tmp_path):
    inst = _instrument(tmp_path)
    inst.write(":MEM:ADDR 0x40000000")
    assert inst.query(":MEM:READ?") == "0x71"
    inst.write(":MEM:ADDR 0x20000000")
    inst.write(":MEM:WRITE 0xDEADBEEF")
    assert inst.read_mem(0x20000000)[0] == 0xDEADBEEF
    inst.write(":REG:SEL pc")
    assert inst.query(":REG:READ?") == "0x8000150"
    inst.write(":REG:SEL r0")
    inst.write(":REG:WRITE 0x42")
    assert inst.read_reg("r0") == 0x42


def test_instrument_flash_updates_firmware_query(tmp_path):
    inst = _instrument(tmp_path)
    fw = tmp_path / "app.bin"
    fw.write_bytes(b"payload-bytes")
    assert inst.query(":FW?") == "sim-1.0.0"
    res = inst.flash(str(fw), chip="sim-target")
    assert res["ok"] is True and res["verified"] is True
    assert inst.query(":FW?") == "flashed sim-1.0.0"        # cache invalidated


def test_instrument_rtt_writes_spool_artifact(tmp_path):
    inst = _instrument(tmp_path)
    res = inst.rtt(0.02)
    assert res["line_count"] == 4 and res["path"].endswith(".txt")
    text = open(res["path"], encoding="utf-8").read()
    assert "wifi_connected" in text and text.endswith("\n")


def test_instrument_gdb_out_writes_file(tmp_path):
    inst = _instrument(tmp_path)
    out = tmp_path / "gdb.log"
    res = inst.gdb("dump.gdb", out=str(out))
    assert res["exit_code"] == 0 and res["path"] == str(out)
    assert "Breakpoint" in out.read_text(encoding="utf-8")


def test_instrument_capture_polls_memory_and_is_bounded(tmp_path):
    inst = _instrument(tmp_path)
    samples = inst.capture("mem:0x40000004", seconds=0.05, interval=0.01)
    assert 0 < len(samples) <= 4096
    assert all(s.units == "count" for s in samples)
    reg = inst.capture("pc", seconds=0.02, interval=0.01)
    assert 0 < len(reg) <= 4096


def test_instrument_unsupported_verbs_raise(tmp_path):
    inst = _instrument(tmp_path)
    with pytest.raises(HilError):
        inst.write(":BOGUS 1")
    with pytest.raises(HilError):
        inst.query(":BOGUS?")


def test_instrument_safety_policy_is_enforced(tmp_path):
    dev = load_registry()["generic-cortex-m"]
    blocked = replace(dev, safety=replace(dev.safety, blocked=(":FLASH", ":ERASE")))
    inst = _instrument(tmp_path, device=blocked)
    with pytest.raises(SafetyViolationError):
        inst.flash("app.bin")
    with pytest.raises(SafetyViolationError):
        inst.write(":ERASE")


# -- real tool backend: pure argv builders (no I/O, no tools installed) ------
def test_probe_rs_command_builders():
    be = ToolBackend("probe-rs", chip="nrf52840")
    assert be._command("probe_list") == ["probe-rs", "list"]
    assert be._command("flash", firmware="app.hex") == \
        ["probe-rs", "download", "--chip", "nrf52840", "app.hex"]
    assert be._command("erase") == ["probe-rs", "erase", "--chip", "nrf52840"]
    assert be._command("reset") == ["probe-rs", "reset", "--chip", "nrf52840"]
    assert be._command("rtt") == ["probe-rs", "attach", "--chip", "nrf52840"]
    assert be._command("read_mem", addr="0x20000000") is None   # not supported


def test_probe_rs_command_includes_probe_serial():
    be = ToolBackend("probe-rs", chip="nrf52840", probe="0001")
    assert be._command("flash", firmware="app.hex") == \
        ["probe-rs", "download", "--chip", "nrf52840", "--probe", "0001", "app.hex"]


def test_openocd_command_builders():
    be = ToolBackend("openocd", target="stm32f4x", interface="stlink")
    cfg = ["openocd", "-f", "interface/stlink.cfg", "-f", "target/stm32f4x.cfg", "-c"]
    assert be._command("flash", firmware="fw.elf", verify=True) == \
        cfg + ["program fw.elf verify reset exit"]
    assert be._command("read_mem", addr="0x8000000", count=4) == \
        cfg + ["init; halt; mdw 0x8000000 4; resume; exit"]
    assert be._command("halt") == cfg + ["init; halt; exit"]
    assert be._command("read_reg", name="pc") == cfg + ["init; halt; reg pc; exit"]


def test_esptool_command_builders():
    be = ToolBackend("esptool", chip="esp32", probe="/dev/cu.usb-1")
    pre = ["esptool.py", "--port", "/dev/cu.usb-1", "--chip", "esp32"]
    assert be._command("flash", firmware="fw.bin", address=0x1000) == \
        pre + ["write_flash", "0x1000", "fw.bin"]
    assert be._command("erase") == pre + ["erase_flash"]
    assert be._command("chip_id") == pre + ["chip_id"]
    assert be._command("reset") == pre + ["run"]


def test_stlink_command_builders():
    be = ToolBackend("stlink", target="stm32f4x")
    assert be._command("probe_list") == ["st-info", "--probe"]
    assert be._command("flash", firmware="fw.bin", address=0x8000000) == \
        ["st-flash", "write", "fw.bin", "0x8000000"]
    assert be._command("erase") == ["st-flash", "erase"]
    assert be._command("reset") == ["st-flash", "reset"]


def test_pyocd_command_builders():
    be = ToolBackend("pyocd", target="cortex_m")
    assert be._command("probe_list") == ["pyocd", "list"]
    assert be._command("flash", firmware="fw.elf") == ["pyocd", "flash", "-t", "cortex_m", "fw.elf"]
    assert be._command("erase") == ["pyocd", "erase", "-t", "cortex_m", "--mass"]
    assert be._command("read_mem", addr="0x20000000", count=2) == \
        ["pyocd", "cmd", "-t", "cortex_m", "-c", "read32 0x20000000 2"]
    assert be._command("step") == ["pyocd", "cmd", "-t", "cortex_m", "-c", "step"]


def test_unknown_tool_raises_and_missing_tool_error_is_actionable():
    with pytest.raises(HilError):
        ToolBackend("bogus-tool")
    err = ToolBackend("stlink")._missing_tool_error()
    assert isinstance(err, HilError) and "hil setup stlink" in str(err)


# -- driver registry + factory ---------------------------------------------
def test_debug_driver_is_registered():
    assert "debug" in available_drivers()
    assert callable(get_driver("debug"))
    assert "erase" in DEBUG_ACTIONS and "rtt" in DEBUG_ACTIONS and "read-mem" in DEBUG_ACTIONS


def test_factory_simulator_backend():
    inst = debug_driver(parse_address("debug://simulator"), None, 5.0)
    assert isinstance(inst, DebugInstrument)
    assert isinstance(inst.backend, DebugSimBackend)


def test_factory_sim_backend_takes_chip_param():
    inst = debug_driver(parse_address("debug://sim?chip=stm32f4"), None, 5.0)
    assert "stm32f4" in inst.idn()


def _resolve_factory_tool(url: str, binary_candidates: tuple[str, ...]) -> str:
    """Return the tool a debug:// URL routed to.

    The factory calls ``backend.open()`` which fails fast when the real tool is
    absent; the actionable error still names the routed tool, so routing is
    assertable with or without the binary installed.
    """
    try:
        inst = debug_driver(parse_address(url), None, 5.0)
        return inst.backend.tool
    except HilError as exc:
        assert any(c in str(exc) for c in binary_candidates), str(exc)
        for cand in binary_candidates:
            if cand in str(exc):
                return cand
        raise


def test_factory_auto_routes_esp_to_esptool():
    tool = _resolve_factory_tool("debug://auto?target=esp32", ("esptool",))
    assert tool == "esptool"


def test_factory_auto_routes_stm32_to_stlink():
    tool = _resolve_factory_tool("debug://auto?target=stm32f4x", ("stlink", "st-flash"))
    assert tool in ("stlink", "st-flash")


def test_tool_backend_param_plumbing_without_open():
    # Constructing a ToolBackend does no I/O; only open() resolves the binary.
    be = ToolBackend("probe-rs", target="nrf52840", probe="0001", transport="swd")
    assert be.tool == "probe-rs" and be.chip == "nrf52840" and be.probe == "0001"
    assert be.transport == "swd" and be.interface == "cmsis-dap"   # non-stlink default
    st = ToolBackend("stlink", target="stm32f4x")
    assert st.interface == "stlink"                               # stlink default
