"""Offline tests for the agentic HIL test bench (hil.bench)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hil.bench import (
    SAMPLE_SPEC,
    SimTransport,
    build_instrument_factory,
    evaluate,
    load_procedure,
    parse_procedure,
    resolve_run_target,
    run_procedure,
    validate_procedure,
)
from hil.cli import main
from hil.errors import ProcedureError, TransportError

EXAMPLES = Path(__file__).parent.parent / "examples" / "bench"


# -- simulator ---------------------------------------------------------------


def test_sim_common_commands():
    sim = SimTransport(idn="ACME,X1,SN9,2.0")
    sim.open()
    assert sim.query("*IDN?") == "ACME,X1,SN9,2.0"
    assert sim.query("*OPC?") == "1"
    assert sim.query("SYST:ERR?") == '0,"NO ERROR"'


def test_sim_response_map_and_default():
    sim = SimTransport({":MEAS:VOLT?": "3.301"}, default_query_reply="42")
    assert sim.query(":MEAS:VOLT?") == "3.301"
    assert sim.query(":meas:volt?") == "3.301"       # case-insensitive
    assert sim.query(":SOMETHING?") == "42"           # default reply
    sim.write(":SOUR:VOLT 3.3")                        # setting: no reply staged
    assert sim.read() == ""


def test_sim_records_and_error_queue():
    sim = SimTransport(error_queue=['-222,"Data out of range"'])
    sim.write("OUTP ON")
    sim.query("*IDN?")
    assert sim.writes == ["OUTP ON", "*IDN?"]
    assert sim.queries == ["*IDN?"]
    assert sim.query("SYST:ERR?") == '-222,"Data out of range"'
    assert sim.query("SYST:ERR?") == '0,"NO ERROR"'   # drained


# -- assertions --------------------------------------------------------------


def test_evaluate_within_and_units():
    assert evaluate("within", 3.301, "3.267..3.333")[0] is True
    assert evaluate("within", 5, "0..200 V")[0] is True
    assert evaluate("within", 999, "0..200 V")[0] is False
    assert evaluate("within", 3.301, [3.0, 3.5])[0] is True
    assert evaluate("within", 3.301, {"min": 3.0, "max": 3.5})[0] is True


def test_evaluate_approx():
    assert evaluate("approx", 1.0, "1.0±0.01")[0] is True
    assert evaluate("approx", 1.5, "1.0±0.01")[0] is False
    assert evaluate("approx", "1.000E+00", [1.0, 0.01])[0] is True


def test_evaluate_numeric_and_string():
    assert evaluate("eq", "3.30", 3.3)[0] is True
    assert evaluate("eq", "ON", "ON")[0] is True
    assert evaluate("ne", "ON", "OFF")[0] is True
    assert evaluate("lt", 0.05, 1.0)[0] is True
    assert evaluate("ge", 5, 5)[0] is True
    assert evaluate("gt", "abc", 1)[0] is False        # non-numeric -> fail, not crash


def test_evaluate_text_ops():
    assert evaluate("contains", "3.301E+00", "E+00")[0] is True
    assert evaluate("matches", "KEITHLEY,2400", r"^\w+,\d+")[0] is True
    assert evaluate("not_matches", "abc", r"\d")[0] is True
    assert evaluate("matches", "abc", "(")[0] is False  # bad regex -> fail, not crash
    assert evaluate("exists", "")[0] is False
    assert evaluate("exists", "x")[0] is True
    assert evaluate("is_true", "1")[0] is True
    assert evaluate("is_false", "0")[0] is True
    assert evaluate("bogus", 1, 1)[0] is False


# -- procedure parsing / validation -----------------------------------------


def test_parse_step_forms():
    proc = parse_procedure(
        {"steps": ["reset", {"write": "A"}, {"measure": {"command": "B?", "save_as": "b"}},
                   {"type": "opc", "save_as": "d"}]}
    )
    assert [s.type for s in proc.steps] == ["reset", "write", "measure", "opc"]
    assert proc.steps[1].args == {"command": "A"}
    assert proc.steps[2].args["save_as"] == "b"
    assert proc.steps[3].args["save_as"] == "d"


def test_parse_metadata_and_list_form():
    proc = parse_procedure(
        {"name": "n", "device": "d", "address": "tcp://h:1", "variables": {"x": 1},
         "continue_on_error": True, "sim": {"responses": {"A?": "1"}},
         "steps": ["reset"]},
        source="s.yaml",
    )
    assert proc.device == "d" and proc.address == "tcp://h:1"
    assert proc.continue_on_error is True
    assert proc.sim.responses == {"A?": "1"}
    assert proc.source == "s.yaml"
    # a bare list of steps is also a valid spec
    assert parse_procedure(["reset", "opc"]).steps[0].type == "reset"


def test_validate_catches_problems():
    assert any("unknown type" in e for e in validate_procedure({"steps": [{"bogus": 1}]}))
    assert any("missing 'command'" in e for e in validate_procedure({"steps": [{"write": {}}]}))
    assert any("unexpected arg" in e for e in validate_procedure(
        {"steps": [{"write": {"command": "A", "bogus": 1}}]}))
    assert any("unknown op" in e for e in validate_procedure(
        {"steps": [{"assert": {"that": "x", "op": "nope"}}]}))
    assert any("needs a 'value'" in e for e in validate_procedure(
        {"steps": [{"assert": {"that": "x", "op": "within"}}]}))
    assert any("no steps" in e for e in validate_procedure({"steps": []}))
    assert any("expect must be" in e for e in validate_procedure(
        {"steps": [{"errors": {"expect": "maybe"}}]}))
    assert validate_procedure({"steps": ["reset"]}) == []


def test_parse_raises_procedure_error():
    with pytest.raises(ProcedureError):
        parse_procedure({"steps": [{"bogus": 1}]})


def test_load_yaml_and_json(tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text("name: fromyaml\nsteps:\n  - reset\n", encoding="utf-8")
    assert load_procedure(y).name == "fromyaml"
    j = tmp_path / "p.json"
    j.write_text(json.dumps({"name": "fromjson", "steps": ["opc"]}), encoding="utf-8")
    assert load_procedure(j).name == "fromjson"


def test_load_missing_and_empty(tmp_path):
    with pytest.raises(ProcedureError):
        load_procedure(tmp_path / "nope.yaml")
    blank = tmp_path / "blank.yaml"
    blank.write_text("", encoding="utf-8")
    with pytest.raises(ProcedureError):
        load_procedure(blank)


# -- runner ------------------------------------------------------------------


def _run(proc, device_slug="", mode="sim", target="simulator", **kw):
    factory = build_instrument_factory(
        proc, mode=mode, target=target, device_slug=device_slug
    )
    return run_procedure(proc, factory, mode=mode, target=target, **kw)


def test_run_sample_passes():
    proc = parse_procedure(yaml.safe_load(SAMPLE_SPEC), source="sample.yaml")
    report = _run(proc, device_slug=proc.device)
    assert report.ok is True
    assert report.counts()["passed"] == 9
    assert {m.name: m.value for m in report.measurements} == {"vout": 3.301, "iout": 0.052}


def test_variable_substitution_and_set():
    proc = parse_procedure(
        {"variables": {"vset": 3.3},
         "sim": {"responses": {":MEAS:VOLT?": "3.3"}},
         "steps": [{"set": {"var": "n", "value": 7}},
                   {"log": "n is ${n}"},
                   {"write": ":SOUR:VOLT ${vset}"},
                   {"assert": {"that": "n", "op": "eq", "value": 7}}]}
    )
    report = _run(proc)
    assert report.ok is True
    assert report.steps[1].message == "n is 7"
    assert report.steps[2].summary == "write :SOUR:VOLT 3.3"


def test_failing_assert_halts_by_default():
    proc = parse_procedure(
        {"steps": [{"measure": {"command": "X?", "save_as": "v"}},
                   {"assert": {"that": "v", "op": "gt", "value": 100}},
                   {"log": "never"}]}
    )
    report = _run(proc)
    assert report.ok is False
    assert report.counts() == {"total": 3, "passed": 1, "failed": 1, "skipped": 1,
                               "measurements": 1}
    assert report.steps[2].skipped is True


def test_continue_on_error_runs_all():
    proc = parse_procedure(
        {"steps": [{"assert": {"that": 1, "op": "eq", "value": 2}},
                   {"assert": {"that": 2, "op": "eq", "value": 2}}]}
    )
    report = _run(proc, continue_on_error=True)
    assert report.counts()["skipped"] == 0
    assert report.steps[0].ok is False and report.steps[1].ok is True


def test_safety_policy_enforced_in_sim():
    proc = parse_procedure(
        {"device": "keithley-2400", "steps": [{"write": "CAL:STR:UNPR 1"}]}
    )
    report = _run(proc, device_slug="keithley-2400")
    assert report.ok is False
    assert "blocked by the device safety policy" in report.steps[0].message


def test_errors_expect_none_fails_on_queued_error():
    proc = parse_procedure(
        {"sim": {"errors": ['-222,"Data out of range"']},
         "steps": [{"errors": {"expect": "none"}}]}
    )
    report = _run(proc)
    assert report.ok is False
    assert report.steps[0].errors[0]["code"] == -222


def test_connection_failure_is_a_failed_report():
    proc = parse_procedure({"steps": ["reset", "opc"]})

    def boom():
        raise TransportError("no route to host")

    report = run_procedure(proc, boom, mode="live", target="tcp://1.2.3.4:5025")
    assert report.ok is False
    assert report.steps[0].type == "connect"
    assert "no route to host" in report.steps[0].message
    assert all(s.skipped for s in report.steps[1:])


def test_wait_opc_idn_query_steps():
    proc = parse_procedure(
        {"sim": {"responses": {":MEAS:VPP?": "3.24"}},
         "steps": [{"wait": {}}, {"opc": {"save_as": "d"}}, {"idn": {"save_as": "i"}},
                   {"query": {"command": ":MEAS:VPP?", "save_as": "vpp"}},
                   {"assert": {"that": "vpp", "op": "eq", "value": "3.24"}}]}
    )
    report = _run(proc)
    assert report.ok is True
    assert report.steps[3].response == "3.24"


# -- report emitters ---------------------------------------------------------


def test_report_serialisation_and_rendering():
    proc = parse_procedure(yaml.safe_load(SAMPLE_SPEC), source="sample.yaml")
    report = _run(proc, device_slug=proc.device)
    payload = json.loads(json.dumps(report.to_dict(), default=str))  # JSON-serialisable
    assert payload["ok"] is True and payload["counts"]["total"] == 9
    assert payload["measurements"][0]["name"] == "vout"
    assert "PASS" in report.human() and "measurements:" in report.human()
    md = report.markdown()
    assert md.startswith("# SMU output accuracy") and "| # | step |" in md


def test_resolve_run_target_modes():
    proc = parse_procedure({"device": "d", "address": "tcp://spec:1", "steps": ["reset"]})
    assert resolve_run_target(proc) == ("live", "tcp://spec:1", "d")
    assert resolve_run_target(proc, sim=True) == ("sim", "simulator", "d")
    assert resolve_run_target(proc, address="tcp://cli:2") == ("live", "tcp://cli:2", "d")
    bare = parse_procedure({"device": "d", "steps": ["reset"]})
    assert resolve_run_target(bare) == ("sim", "simulator", "d")   # no address -> dry-run


# -- CLI ---------------------------------------------------------------------


def test_cli_bench_init_and_validate(tmp_path, capsys):
    spec = tmp_path / "ex.yaml"
    assert main(["bench", "init", str(spec)]) == 0
    assert spec.exists() and "SMU output accuracy" in spec.read_text()
    capsys.readouterr()  # drain the human output
    # refuse to clobber without --force
    assert main(["bench", "init", str(spec), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert main(["bench", "init", str(spec), "--force"]) == 0
    capsys.readouterr()
    assert main(["bench", "validate", str(spec), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_bench_validate_invalid(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("steps:\n  - bogus: 1\n", encoding="utf-8")
    assert main(["bench", "validate", str(bad), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_bench_list_json(tmp_path, capsys):
    spec = tmp_path / "s.yaml"
    main(["bench", "init", str(spec)])
    capsys.readouterr()
    assert main(["bench", "list", str(spec), "--json"]) == 0
    steps = json.loads(capsys.readouterr().out)["steps"]
    assert steps[0]["type"] == "reset" and len(steps) == 9


def test_cli_bench_run_sim_json_and_report_out(tmp_path, capsys):
    spec = tmp_path / "s.yaml"
    main(["bench", "init", str(spec)])
    capsys.readouterr()
    report = tmp_path / "report.json"
    code = main(["bench", "run", str(spec), "--json", "--report-out", str(report)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True and payload["mode"] == "sim"
    assert report.exists() and json.loads(report.read_text())["ok"] is True


def test_cli_bench_run_fail_exit_code(tmp_path, capsys):
    spec = tmp_path / "fail.yaml"
    spec.write_text(
        "steps:\n  - measure: {command: 'X?', save_as: v}\n"
        "  - assert: {that: v, op: gt, value: 100}\n",
        encoding="utf-8",
    )
    assert main(["bench", "run", str(spec)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_bench_run_live(fake_address, tmp_path, capsys):
    spec = tmp_path / "live.yaml"
    spec.write_text(
        "name: live\nsteps:\n"
        "  - idn: {save_as: idn}\n"
        "  - measure: {command: 'MEAS:VOLT?', save_as: v, units: V}\n"
        "  - assert: {that: v, op: within, value: '3.0..3.2'}\n"
        "  - errors: {expect: none}\n",
        encoding="utf-8",
    )
    code = main(["bench", "run", str(spec), "--address", fake_address, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["mode"] == "live" and payload["ok"] is True
    assert payload["measurements"][0]["value"] == pytest.approx(3.14159)


def test_bundled_examples_are_valid():
    specs = sorted(EXAMPLES.glob("*.yaml"))
    assert specs, "no bundled example procedures found"
    for path in specs:
        errs = validate_procedure(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert not errs, f"{path.name}: {errs}"


def test_bundled_examples_pass_on_simulator():
    for path in sorted(EXAMPLES.glob("*.yaml")):
        proc = load_procedure(path)
        report = _run(proc, device_slug=proc.device)
        assert report.ok is True, f"{path.name} failed: {report.human()}"
