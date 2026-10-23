"""Bench-level tests for the logic-analyzer ``logic`` and ``decode`` steps.

These drive the runner (:func:`~hil.bench.runner.run_procedure`) end to end on the
built-in simulator: a ``logic`` step produces a file-backed artifact, a ``decode``
step turns it into a decoded artifact, both are threaded into the report, and
``${var}`` substitution + validation behave. Driver/backend unit tests live in
``test_drivers_logic.py``; the bundled ``atk-logic-i2c-decode.yaml`` example is
covered by ``test_bench.py``.
"""

from __future__ import annotations

from pathlib import Path

from hil.bench import (
    build_instrument_factory,
    parse_procedure,
    run_procedure,
    validate_procedure,
)

DEVICE = "alientek-atk-logic"


def _run(proc, device_slug=DEVICE):
    factory = build_instrument_factory(
        proc, mode="sim", target="simulator", device_slug=device_slug
    )
    return run_procedure(proc, factory, mode="sim", target="simulator")


# -- execution --------------------------------------------------------------
def test_logic_step_records_artifact():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"write": ":CONF:CHAN 0,1"},
            {"logic": {"seconds": 0.05, "channels": [0, 1], "sample_rate_hz": 1e6,
                       "format": "csv", "save_as": "wave"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert report.counts()["artifacts"] == 1
    art = report.artifacts[0]
    assert art["kind"] == "logic" and art["format"] == "csv"
    assert art["sample_count"] > 0 and art["channels"] == [0, 1]
    assert Path(art["path"]).exists()
    # the step's value is the artifact dict (same path)
    assert report.steps[1].value["path"] == art["path"]


def test_logic_then_decode_and_assert():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"logic": {"seconds": 0.05, "format": "csv", "save_as": "wave"}},
            {"decode": {"decoder": "i2c", "map": {"scl": 0, "sda": 1},
                        "format": "csv", "save_as": "frames"}},
            {"assert": {"that": "frames", "op": "gt", "value": 0,
                        "message": "decoder produced frames"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert [a["kind"] for a in report.artifacts] == ["logic", "decoded"]
    decoded = report.artifacts[1]
    assert decoded["frames"] > 0
    assert decoded["parent"] == report.artifacts[0]["id"]   # linked to its source
    assert decoded["decoders"] == ["i2c"]


def test_logic_step_uses_variable_substitution():
    proc = parse_procedure({
        "device": DEVICE,
        "variables": {"rate": 2000000, "window": 0.05},
        "steps": [
            {"logic": {"seconds": "${window}", "sample_rate_hz": "${rate}",
                       "format": "csv"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    art = report.artifacts[0]
    assert art["sample_rate_hz"] == 2000000
    assert art["sample_count"] == 100000            # 2 MS/s * 0.05 s


def test_logic_out_retains_artifact_to_path(tmp_path):
    out = tmp_path / "captures" / "wave.csv"
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{"logic": {"seconds": 0.02, "format": "csv", "out": str(out)}}],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    assert out.exists() and out.stat().st_size > 0
    assert report.artifacts[0]["path"] == str(out)


def test_decode_input_resolves_saved_variable():
    # `save_as` on the logic step stores its path; `decode.input` reads it back.
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"logic": {"seconds": 0.02, "format": "csv", "save_as": "wave"}},
            {"decode": {"decoder": "i2c", "input": "wave", "format": "csv"}},
        ],
    })
    report = _run(proc)
    assert report.ok is True, report.human()
    wave, decoded = report.artifacts
    assert decoded["kind"] == "decoded" and decoded["frames"] > 0
    # decoding an explicit path (not the in-memory artifact) yields a new file
    assert decoded["path"] != wave["path"] and Path(decoded["path"]).exists()


# -- summaries --------------------------------------------------------------
def test_logic_and_decode_step_summaries():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [
            {"logic": {"seconds": 0.5, "channels": [0, 1], "save_as": "wave"}},
            {"decode": {"decoder": "i2c", "save_as": "frames"}},
        ],
    })
    logic_sum = proc.steps[0].summary()
    assert "logic capture" in logic_sum and "0.5s" in logic_sum
    assert "-> wave" in logic_sum
    assert proc.steps[1].summary() == "decode i2c -> frames"


# -- guardrails -------------------------------------------------------------
def test_logic_step_needs_a_logic_instrument():
    # A `logic` step pointed at a plain SCPI instrument fails with a clear hint
    # (this is what happens when --address targets a non-analyzer device).
    scpi_proc = parse_procedure({"steps": [{"log": "x"}]})   # no logic steps
    factory = build_instrument_factory(
        scpi_proc, mode="sim", target="simulator", device_slug=""
    )
    logic_proc = parse_procedure({"steps": [{"logic": {"seconds": 0.05}}]})
    report = run_procedure(logic_proc, factory, mode="sim", target="simulator")
    assert report.ok is False
    assert "logic-analyzer instrument" in report.steps[0].message


def test_logic_validation_needs_a_capture_size():
    errs = validate_procedure({"steps": [{"logic": {}}]})
    assert any("capture size" in e for e in errs)


def test_logic_validation_rejects_non_numeric_size():
    errs = validate_procedure({"steps": [{"logic": {"seconds": "abc"}}]})
    assert any("must be a number" in e for e in errs)


def test_logic_validation_accepts_variable_size():
    assert validate_procedure({"steps": [{"logic": {"seconds": "${window}"}}]}) == []


def test_decode_validation_needs_a_decoder():
    errs = validate_procedure({"steps": [{"decode": {}}]})
    assert any("decoder" in e for e in errs)


# -- report serialization ---------------------------------------------------
def test_report_serializes_artifacts():
    proc = parse_procedure({
        "device": DEVICE,
        "steps": [{"logic": {"seconds": 0.02, "format": "csv"}}],
    })
    report = _run(proc)
    d = report.to_dict()
    assert d["counts"]["artifacts"] == 1
    assert d["artifacts"][0]["kind"] == "logic"
    assert "artifact logic:" in report.human()
    assert "**Artifacts:**" in report.markdown()
