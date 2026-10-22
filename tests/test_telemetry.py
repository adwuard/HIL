"""Tests for telemetry: Sample, durable logging, and analytics."""

from __future__ import annotations

import json

from hil.telemetry import DataLogger, Sample, iter_channels, read_samples, summarize


def _samples(n=5, ch=":MEAS:CURR?", units="A", base=1000.0):
    return [Sample(ts=base + i, channel=ch, value=float(i), units=units) for i in range(n)]


def test_summarize_basic_stats():
    s = summarize(_samples(5))
    assert s["count"] == 5
    assert s["mean"] == 2.0
    assert s["min"] == 0.0 and s["max"] == 4.0
    assert s["duration_s"] == 4.0
    assert s["rate_hz"] == 1.0          # (5-1) intervals / 4 s
    assert s["units"] == "A"
    assert s["p50"] == 2.0


def test_summarize_empty_is_safe():
    s = summarize([])
    assert s["count"] == 0 and s["mean"] is None


def test_summarize_energy_for_power_channel():
    samples = [
        Sample(ts=0.0, channel="power", value=1.0, units="W"),
        Sample(ts=2.0, channel="power", value=1.0, units="W"),
    ]
    s = summarize(samples)
    assert s["energy_j"] == 2.0                     # 1 W x 2 s
    assert s["energy_wh"] == round(2.0 / 3600.0, 6)


def test_datalogger_ndjson_roundtrip(tmp_path):
    p = tmp_path / "run.ndjson"
    with DataLogger(p) as log:
        log.writemany(_samples(3))
    back = read_samples(p)
    assert len(back) == 3
    assert back[0].channel == ":MEAS:CURR?" and back[0].value == 0.0
    assert all(json.loads(line) for line in p.read_text().strip().splitlines())


def test_datalogger_csv_roundtrip(tmp_path):
    p = tmp_path / "run.csv"
    with DataLogger(p) as log:
        log.writemany(_samples(3, units="V"))
    assert p.read_text().startswith("ts,channel,value,units")
    back = read_samples(p)
    assert len(back) == 3 and back[1].value == 1.0 and back[0].units == "V"


def test_datalogger_appends_across_sessions(tmp_path):
    p = tmp_path / "run.ndjson"
    with DataLogger(p) as log:
        log.writemany(_samples(2))
    with DataLogger(p) as log:
        log.writemany(_samples(2))
    assert len(read_samples(p)) == 4


def test_iter_channels_groups_by_channel():
    mixed = _samples(2, ch="a") + _samples(3, ch="b")
    groups = dict(iter_channels(mixed))
    assert set(groups) == {"a", "b"}
    assert len(groups["a"]) == 2 and len(groups["b"]) == 3


def test_read_samples_missing_file_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        read_samples(tmp_path / "nope.ndjson")
