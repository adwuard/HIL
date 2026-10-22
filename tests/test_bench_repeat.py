"""Tests for the bench `repeat` loop step and the PPK2 battery-soak example."""

from __future__ import annotations

from pathlib import Path

from hil.bench import (
    build_instrument_factory,
    load_procedure,
    parse_procedure,
    run_procedure,
    validate_procedure,
)
from hil.telemetry import read_samples

EXAMPLES = Path(__file__).parent.parent / "examples" / "bench"


def _run(proc, device_slug=""):
    factory = build_instrument_factory(
        proc, mode="sim", target="simulator", device_slug=device_slug
    )
    return run_procedure(proc, factory, mode="sim", target="simulator")


def test_repeat_runs_body_count_times():
    proc = parse_procedure(
        {
            "sim": {"responses": {":MEAS:CURR?": "0.05"}},
            "steps": [
                {"repeat": {"count": 3, "steps": [
                    {"write": ":OUTP ON"},
                    {"capture": {"channel": ":MEAS:CURR?", "seconds": 0.05,
                                 "interval": 0.02, "units": "A", "save_as": "i"}},
                    {"write": ":OUTP OFF"},
                ]}},
            ],
        }
    )
    report = _run(proc)
    assert report.ok is True, report.human()
    # One summary step at the top level, but the body captured once per cycle.
    assert len(report.to_dict()["steps"]) == 1
    assert report.to_dict()["steps"][0]["type"] == "repeat"
    assert len(report.captures) == 3
    assert report.to_dict()["steps"][0]["value"] == {"count": 3, "cycles": 3, "failures": 0}


def test_repeat_uses_variable_count():
    proc = parse_procedure(
        {
            "variables": {"n": 4},
            "sim": {"responses": {":MEAS:CURR?": "0.05"}},
            "steps": [
                {"repeat": {"count": "${n}", "steps": [
                    {"capture": {"channel": ":MEAS:CURR?", "seconds": 0.03,
                                 "interval": 0.01, "units": "A"}},
                ]}},
            ],
        }
    )
    report = _run(proc)
    assert report.ok is True, report.human()
    assert len(report.captures) == 4


def test_repeat_nested_failure_halts():
    proc = parse_procedure(
        {
            "sim": {"responses": {":MEAS:CURR?": "0.05"}},
            "steps": [
                {"repeat": {"count": 5, "steps": [
                    {"measure": {"command": ":MEAS:CURR?", "save_as": "i"}},
                    {"assert": {"that": "i", "op": "gt", "value": 1.0}},  # 0.05 > 1.0 -> fail
                ]}},
            ],
        }
    )
    report = _run(proc)
    assert report.ok is False
    summary = report.to_dict()["steps"][-1]
    assert summary["type"] == "repeat"
    assert summary["value"]["failures"] >= 1
    assert summary["value"]["cycles"] == 1  # halted after the first cycle


def test_repeat_validation():
    assert any("'count'" in e for e in
               validate_procedure({"steps": [{"repeat": {"steps": ["reset"]}}]}))
    assert any("non-empty list" in e for e in
               validate_procedure({"steps": [{"repeat": {"count": 2, "steps": []}}]}))
    nested = validate_procedure(
        {"steps": [{"repeat": {"count": 2, "steps": [{"write": {}}]}}]}
    )
    assert any("repeat" in e and "command" in e for e in nested)
    assert validate_procedure(
        {"steps": [{"repeat": {"count": 2, "steps": ["reset"]}}]}
    ) == []


def test_seconds_accepts_variable_reference():
    # `seconds: ${var}` must validate (resolved at run time), "abc" must not.
    assert validate_procedure(
        {"steps": [{"capture": {"channel": "x", "seconds": "${period_s}"}}]}
    ) == []
    assert any("must be a number" in e for e in
               validate_procedure({"steps": [{"capture": {"channel": "x", "seconds": "abc"}}]}))


def test_soak_example_runs_scaled_on_simulator(tmp_path):
    proc = load_procedure(EXAMPLES / "ppk2-battery-soak.yaml")
    assert proc.long_running is True
    assert proc.device == "nordic-ppk2"
    # Scale the hour-long soak down to a fast, hardware-free dry-run.
    proc.variables.update({
        "cycles": 2, "period_s": 0.04, "sample_s": 0.02,
        "flag_s": 0.02, "log_dir": str(tmp_path),
    })
    report = _run(proc, device_slug=proc.device)
    assert report.ok is True, report.human()
    # 2 cycles x (current-on + battery-flag + current-off) = 6 captures.
    assert len(report.captures) == 6
    on_file = tmp_path / "current-on.ndjson"
    assert on_file.exists() and len(read_samples(on_file)) >= 2
    assert (tmp_path / "battery-flag.ndjson").exists()
    assert (tmp_path / "current-off.ndjson").exists()
