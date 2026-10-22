"""Tests for the bench `capture` step (telemetry inside procedures)."""

from __future__ import annotations

import pytest

from hil.bench import (
    build_instrument_factory,
    parse_procedure,
    run_procedure,
    validate_procedure,
)
from hil.telemetry import read_samples


def _run(proc, device_slug=""):
    factory = build_instrument_factory(
        proc, mode="sim", target="simulator", device_slug=device_slug
    )
    return run_procedure(proc, factory, mode="sim", target="simulator")


def test_capture_step_records_stats_and_feeds_assert():
    proc = parse_procedure(
        {
            "sim": {"responses": {":MEAS:CURR?": "0.05"}},
            "steps": [
                {"capture": {"channel": ":MEAS:CURR?", "seconds": 0.15,
                             "interval": 0.05, "units": "A", "save_as": "avg"}},
                {"assert": {"that": "avg", "op": "within", "value": "0.049..0.051"}},
            ],
        }
    )
    report = _run(proc)
    assert report.ok is True, report.human()
    assert len(report.captures) == 1
    cap = report.captures[0]
    assert cap["count"] >= 2 and cap["units"] == "A"
    assert cap["mean"] == pytest.approx(0.05)
    assert report.to_dict()["captures"][0]["channel"] == ":MEAS:CURR?"


def test_capture_writes_sidecar_file(tmp_path):
    out = tmp_path / "cap.ndjson"
    proc = parse_procedure(
        {
            "sim": {"responses": {":MEAS:VOLT?": "3.3"}},
            "steps": [{"capture": {"channel": ":MEAS:VOLT?", "seconds": 0.12,
                                   "interval": 0.04, "units": "V", "out": str(out)}}],
        }
    )
    report = _run(proc)
    assert report.ok is True, report.human()
    assert out.exists()
    assert len(read_samples(out)) >= 2
    assert report.captures[0]["file"] == str(out)


def test_capture_appears_in_human_and_markdown():
    proc = parse_procedure(
        {
            "sim": {"responses": {":MEAS:CURR?": "0.05"}},
            "steps": [{"capture": {"channel": ":MEAS:CURR?", "seconds": 0.1,
                                   "interval": 0.05, "units": "A"}}],
        }
    )
    report = _run(proc)
    assert "capture :MEAS:CURR?" in report.human()
    assert "| channel | n | mean |" in report.markdown()


def test_capture_validation():
    assert any("missing 'channel'" in e for e in
               validate_procedure({"steps": [{"capture": {"seconds": 1}}]}))
    assert any("must be a number" in e for e in
               validate_procedure({"steps": [{"capture": {"channel": "x", "seconds": "abc"}}]}))
    assert validate_procedure(
        {"steps": [{"capture": {"channel": "x", "seconds": 1}}]}
    ) == []
