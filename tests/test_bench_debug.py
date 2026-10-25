"""Bench-level tests for the hardware-in-the-loop ``build``/``flash``/``debug`` steps.

These drive the runner (:func:`~hil.bench.runner.run_procedure`) end to end on the
built-in debug simulator (an in-memory Cortex-M target), so the whole
``build -> flash -> debug/run -> capture -> assert -> report`` pipeline of spec
§5.11 runs with no probe and no toolchain installed. Driver/backend unit tests
live in ``test_drivers_debug.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hil.bench import (
    build_instrument_factory,
    parse_procedure,
    run_procedure,
    validate_procedure,
)

DEVICE = "generic-cortex-m"


def _run(proc, device_slug=DEVICE):
    factory = build_instrument_factory(
        proc, mode="sim", target="simulator", device_slug=device_slug
    )
    return run_procedure(proc, factory, mode="sim", target="simulator")


@pytest.fixture
def firmware(tmp_path):
    fw = tmp_path / "app.bin"
    fw.write_bytes(b"\x00\x01\x02\x03firmware-image")
    return str(fw)


# -- build ------------------------------------------------------------------
def test_build_records_firmware_fingerprint(firmware):
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{"build": {"artifact": firmware, "version": "1.2.3", "save_as": "fw"}}],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.counts()["artifacts"] == 1
    art = report.artifacts[0]
    assert art["kind"] == "firmware" and art["version"] == "1.2.3"
    assert len(art["sha256"]) == 64 and art["size_bytes"] > 0
    assert report.steps[0].value["sha256"] == art["sha256"]


def test_build_runs_a_compile_command(tmp_path):
    out = tmp_path / "built.bin"
    # A portable "compile": write some bytes, then fingerprint the result.
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{
            "build": {
                "command": f'{_python()} -c "open(r\'{out}\',\'wb\').write(b\'ELF\')"',
                "artifact": str(out),
                "version": "0.0.1",
            },
        }],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    art = report.artifacts[0]
    assert art["kind"] == "firmware" and art["size_bytes"] == 3
    assert art.get("command") and out.exists()


def _python():
    import sys
    return sys.executable


# -- flash ------------------------------------------------------------------
def test_flash_step_records_flash_artifact(firmware):
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{"flash": {"firmware": firmware, "chip": "nrf52840", "save_as": "sha"}}],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    art = report.artifacts[0]
    assert art["kind"] == "flash" and art["ok"] is True and art["verified"] is True
    assert art["tool"] == "simulator" and art["chip"] == "nrf52840"
    assert len(art["sha256"]) == 64
    assert "flashed app.bin" in report.steps[0].response


def test_build_then_flash_chains_via_variable(firmware):
    # build.save_as -> flash.firmware via ${fw}; flash.save_as -> assert.
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"build": {"artifact": firmware, "version": "2.0.0", "save_as": "fw"}},
            {"flash": {"firmware": "${fw}", "chip": "nrf52840", "save_as": "sha"}},
            {"assert": {"that": "sha", "op": "ne", "value": "",
                        "message": "flash recorded a firmware hash"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert [a["kind"] for a in report.artifacts] == ["firmware", "flash"]
    # the flashed image is byte-identical to the built one
    assert report.artifacts[0]["sha256"] == report.artifacts[1]["sha256"]


# -- debug: run control + memory/registers ---------------------------------
def test_debug_run_control_and_state():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"debug": {"action": "reset", "halt": True}},
            {"debug": {"action": "step"}},
            {"debug": {"action": "resume"}},
            {"debug": {"action": "halt"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert "state=halted" in report.steps[0].response      # reset --halt
    assert "pc=0x8000152" in report.steps[1].response       # stepped from 0x8000150
    assert "state=running" in report.steps[2].response
    assert "state=halted" in report.steps[3].response


def test_debug_read_mem_with_match_assertion():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"debug": {"action": "read-mem", "address": "0x40000000", "match": "0x71"}},
            {"debug": {"action": "write-mem", "address": "0x20000000", "value": "0xDEADBEEF"}},
            {"debug": {"action": "read-mem", "address": "0x20000000", "match": "0xdeadbeef"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.steps[0].response == "0x71"               # WHO_AM_I
    assert report.steps[2].response == "0xdeadbeef"         # written value read back


def test_debug_read_mem_match_failure_marks_step():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{"debug": {"action": "read-mem", "address": "0x40000000", "match": "0x99"}}],
    })
    report = _run(proc)
    assert report.ok is False
    assert "expected to find '0x99'" in report.steps[0].message


def test_debug_read_and_write_registers():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"debug": {"action": "read-reg", "register": "pc"}},
            {"debug": {"action": "write-reg", "register": "r0", "value": "0x42"}},
            {"debug": {"action": "read-reg", "register": "r0", "match": "0x42"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.steps[0].response == "pc = 0x8000150"
    assert report.steps[2].response == "r0 = 0x42"


def test_debug_probe_and_info():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"debug": {"action": "probe"}},
            {"debug": {"action": "info"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.steps[0].response == "1 probe(s)"
    assert "chip=sim-target" in report.steps[1].response
    assert "core=cortex-m4" in report.steps[1].response


def test_debug_rtt_and_gdb_record_artifacts(tmp_path):
    script = tmp_path / "dump.gdb"
    script.write_text("target extended-remote :3333\nmonitor reset halt\n", encoding="utf-8")
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"debug": {"action": "rtt", "duration_s": 0.02,
                       "match": "wifi_connected", "save_as": "log"}},
            {"debug": {"action": "gdb", "script": str(script), "save_as": "gdbout"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert [a["kind"] for a in report.artifacts] == ["rtt", "gdb"]
    rtt = report.artifacts[0]
    assert rtt["line_count"] == 4 and Path(rtt["path"]).exists()
    assert report.artifacts[1]["exit_code"] == 0


def test_debug_erase_records_artifact():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{"debug": {"action": "erase", "chip": "nrf52840"}}],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.artifacts[0]["kind"] == "erase"
    assert report.artifacts[0]["ok"] is True


# -- the full §5.11 pipeline ------------------------------------------------
def test_full_build_flash_debug_pipeline(firmware, tmp_path):
    script = tmp_path / "verify.gdb"
    script.write_text("monitor reset halt\nprint/x *0x40000000\n", encoding="utf-8")
    proc = parse_procedure({
        "device": DEVICE,
        "variables": {"whoami": "0x40000000"},
        "steps": [
            {"build": {"artifact": firmware, "version": "3.1.4", "save_as": "fw"}},
            {"flash": {"firmware": "${fw}", "chip": "nrf52840", "verify": True,
                       "save_as": "sha"}},
            {"debug": {"action": "reset", "halt": True}},
            {"debug": {"action": "read-mem", "address": "${whoami}", "match": "0x71"}},
            {"debug": {"action": "read-reg", "register": "pc"}},
            {"debug": {"action": "step"}},
            {"debug": {"action": "rtt", "duration_s": 0.02, "match": "ready",
                       "save_as": "bootlog"}},
            {"debug": {"action": "gdb", "script": str(script), "save_as": "gdb"}},
            {"debug": {"action": "probe"}},
            {"assert": {"that": "sha", "op": "ne", "value": "",
                        "message": "firmware hash recorded"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.counts()["passed"] == 10
    assert [a["kind"] for a in report.artifacts] == ["firmware", "flash", "rtt", "gdb"]
    # artifacts render in both report formats
    human = report.human()
    for kind in ("firmware", "flash", "rtt", "gdb"):
        assert f"artifact {kind}:" in human
    assert "**Artifacts:**" in report.markdown()


# -- summaries --------------------------------------------------------------
def test_build_flash_debug_step_summaries(firmware):
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"build": {"artifact": firmware, "version": "1.0", "save_as": "fw"}},
            {"flash": {"firmware": firmware, "chip": "nrf52840", "erase": True}},
            {"debug": {"action": "read-mem", "address": "0x40000000"}},
        ],
    })
    assert proc.steps[0].summary().startswith("build ")
    assert "v=1.0" in proc.steps[0].summary() and "-> fw" in proc.steps[0].summary()
    flash_sum = proc.steps[1].summary()
    assert flash_sum.startswith("flash ") and "chip=nrf52840" in flash_sum and "erase" in flash_sum
    assert proc.steps[2].summary() == "debug read-mem 0x40000000"


# -- guardrails -------------------------------------------------------------
def test_flash_step_needs_a_debug_instrument():
    # A `flash` step against a plain SCPI simulator fails with a clear hint.
    scpi_proc = parse_procedure({"steps": [{"log": "x"}]})     # no debug steps
    factory = build_instrument_factory(
        scpi_proc, mode="sim", target="simulator", device_slug=""
    )
    flash_proc = parse_procedure({"steps": [{"flash": {"firmware": "app.bin"}}]})
    report = run_procedure(flash_proc, factory, mode="sim", target="simulator")
    assert report.ok is False
    assert "debug instrument" in report.steps[0].message


def test_debug_step_needs_a_debug_instrument():
    scpi_proc = parse_procedure({"steps": [{"log": "x"}]})
    factory = build_instrument_factory(
        scpi_proc, mode="sim", target="simulator", device_slug=""
    )
    dbg_proc = parse_procedure({"steps": [{"debug": {"action": "halt"}}]})
    report = run_procedure(dbg_proc, factory, mode="sim", target="simulator")
    assert report.ok is False
    assert "debug instrument" in report.steps[0].message


# -- validation -------------------------------------------------------------
def test_build_validation_needs_command_or_artifact():
    errs = validate_procedure({"steps": [{"build": {}}]})
    assert any("build" in e and "command" in e for e in errs)


def test_flash_validation_needs_firmware():
    errs = validate_procedure({"steps": [{"flash": {}}]})
    assert any("firmware" in e for e in errs)


def test_flash_validation_rejects_bad_address():
    errs = validate_procedure({"steps": [{"flash": {"firmware": "f.bin", "address": "nope"}}]})
    assert any("address" in e for e in errs)


def test_flash_validation_accepts_hex_and_var_address():
    assert validate_procedure({"steps": [{"flash": {"firmware": "f.bin", "address": "0x08000000"}}]}) == []
    assert validate_procedure({"steps": [{"flash": {"firmware": "f.bin", "address": "${base}"}}]}) == []


def test_debug_validation_rejects_unknown_action():
    errs = validate_procedure({"steps": [{"debug": {"action": "teleport"}}]})
    assert any("unknown action" in e for e in errs)


def test_debug_validation_action_requirements():
    assert any("address" in e for e in validate_procedure({"steps": [{"debug": {"action": "read-mem"}}]}))
    assert any("register" in e for e in validate_procedure({"steps": [{"debug": {"action": "read-reg"}}]}))
    assert any("value" in e for e in validate_procedure({"steps": [{"debug": {"action": "write-mem", "address": "0x0"}}]}))
    assert any("script" in e for e in validate_procedure({"steps": [{"debug": {"action": "gdb"}}]}))


def test_debug_validation_accepts_variable_numbers():
    assert validate_procedure({"steps": [
        {"debug": {"action": "read-mem", "address": "${addr}", "count": "${n}"}}
    ]}) == []
