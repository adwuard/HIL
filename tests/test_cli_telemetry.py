"""Tests for `hil log` (capture) and `hil analyze` (telemetry analytics)."""

from __future__ import annotations

import json

import pytest

from hil.cli import main
from hil.telemetry import read_samples


def test_cli_log_sim_writes_capture(tmp_path, capsys):
    out = tmp_path / "log.ndjson"
    code = main([
        "log", "keithley-2400", "--channel", ":MEAS:CURR?", "--seconds", "0.15",
        "--interval", "0.05", "--units", "A", "--sim", "--sim-value", "0.05",
        "--out", str(out), "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["captured"] >= 2
    assert payload["stats"]["mean"] == pytest.approx(0.05)
    assert out.exists() and len(read_samples(out)) >= 2


def test_cli_analyze_json(tmp_path, capsys):
    out = tmp_path / "log.ndjson"
    main([
        "log", "keithley-2400", "--channel", ":MEAS:CURR?", "--seconds", "0.12",
        "--interval", "0.04", "--units", "A", "--sim", "--sim-value", "0.05",
        "--out", str(out), "--json",
    ])
    capsys.readouterr()
    code = main(["analyze", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["count"] >= 2
    assert payload["channels"][":MEAS:CURR?"]["mean"] == pytest.approx(0.05)


def test_cli_analyze_human_and_md(tmp_path, capsys):
    out = tmp_path / "log.csv"
    main([
        "log", "keithley-2400", "--channel", ":MEAS:CURR?", "--seconds", "0.1",
        "--interval", "0.05", "--units", "A", "--sim", "--sim-value", "1.0",
        "--out", str(out),
    ])
    capsys.readouterr()
    assert main(["analyze", str(out)]) == 0
    human = capsys.readouterr().out
    assert "samples" in human and ":MEAS:CURR?" in human
    assert main(["analyze", str(out), "--md"]) == 0
    assert "| channel |" in capsys.readouterr().out
