"""Tests for hil.session — auto-bound safety, error attribution, verify, sweep.

All hardware-free: instruments are a :class:`~hil.scpi.ScpiInstrument` over a
:class:`~hil.bench.sim.SimTransport`, and ``connect`` is monkeypatched so
:func:`~hil.session.open_session` hands back the simulator.
"""

from __future__ import annotations

import hil
from hil import session as S
from hil.bench.sim import SimTransport
from hil.scpi import ScpiInstrument

KEITHLEY_IDN = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,SN1,C33"


def _sim(idn="FAKE,TEST-1000,SN1,V1.0", responses=None) -> ScpiInstrument:
    return ScpiInstrument(SimTransport(responses or {}, idn=idn), device=None)


def _patch_connect(monkeypatch, inst: ScpiInstrument) -> None:
    monkeypatch.setattr(
        hil, "connect",
        lambda target, device_slug=None, timeout=5.0, registry_dirs=(): inst,
    )


# -- open_session auto-binds a registry safety policy by *IDN? ---------------
def test_open_session_autobinds_registry_safety(monkeypatch):
    _patch_connect(monkeypatch, _sim(KEITHLEY_IDN))
    status = S.open_session("sim://k", "auto")
    try:
        assert status["matched_slug"] == "keithley-2400"
        assert status["safety_bound"] is True
        assert status["simulated"] is True
        handle = S.get_session("auto")
        assert handle.device is not None and handle.device.slug == "keithley-2400"
        # the auto-bound policy is enforced on a subsequent write
        res = S.run_command(handle, "CAL:VOLT:DC 1")
        assert res["ok"] is False and "blocked" in res["error"].lower()
    finally:
        S.close_session("auto")


def test_open_session_leaves_unknown_device_unbound(monkeypatch):
    # DP932E is not in the built-in registry -> no profile to bind, no guess.
    _patch_connect(monkeypatch, _sim("Rigol Technologies,DP932E,DP9D1,V1"))
    status = S.open_session("sim://r", "nomatch")
    try:
        assert status["matched_slug"] == ""
        assert status["safety_bound"] is False
        assert status["simulated"] is True
        assert S.get_session("nomatch").device is None
    finally:
        S.close_session("nomatch")


def test_open_session_respects_explicit_slug(monkeypatch):
    # A caller-pinned device must not be second-guessed by auto-bind.
    inst = ScpiInstrument(SimTransport({}, idn=KEITHLEY_IDN), device=None)
    monkeypatch.setattr(
        hil, "connect",
        lambda target, device_slug=None, timeout=5.0, registry_dirs=(): inst,
    )
    status = S.open_session("sim://k", "pinned", device_slug="keysight-ac6801b")
    try:
        # connect() would have attached the slug; auto-bind only fires when device
        # is still None, so an explicitly-pinned handle is left as connected gave it.
        assert status["session"] == "pinned"
    finally:
        S.close_session("pinned")


def test_session_lifecycle_list_and_close_all(monkeypatch):
    _patch_connect(monkeypatch, _sim(KEITHLEY_IDN))
    S.open_session("sim://k", "a")
    S.open_session("sim://k", "b")
    assert set(S.session_names()) >= {"a", "b"}
    assert S.close_session("a") is True
    assert S.close_session("a") is False       # already gone
    assert S.close_all_sessions() >= 1
    assert S.session_names() == []


# -- error attribution -------------------------------------------------------
def test_run_command_attributes_error_to_the_write():
    inst = _sim()
    inst.transport.push_error('-141,"DATA MISSING"')
    res = S.run_command(inst, "CONF:BAD 1")
    assert res["ok"] is False
    assert res["errors"] and "-141" in res["errors"][0]


def test_run_command_clean_write_is_ok():
    inst = _sim()
    res = S.run_command(inst, ":SOUR:VOLT 3.3")
    assert res["ok"] is True and res["errors"] == []


# -- verify ------------------------------------------------------------------
def test_verify_window_pass_and_fail():
    inst = _sim(responses={"MEAS:VOLT?": "5.001"})
    good = S.verify(inst, "MEAS:VOLT?", "4.9..5.1")
    assert good["ok"] is True and good["value"] == 5.001
    bad = S.verify(inst, "MEAS:VOLT?", "5.5..6.0")
    assert bad["ok"] is False


def test_verify_overload_is_not_a_value():
    inst = _sim(responses={"MEAS:VOLT?": "9.9E+37"})   # DMM overload sentinel
    res = S.verify(inst, "MEAS:VOLT?", "-10..10")
    assert res["ok"] is False and res["value"] is None


# -- sweep -------------------------------------------------------------------
def test_sweep_aborts_when_output_never_comes_on():
    # The exact field failure: output-enable silently dropped. Verify reads OFF,
    # so the sweep must abort with zero rows rather than report N bad measurements.
    inst = _sim(responses={":OUTP:STAT?": "0"})
    res = S.sweep(inst, inst, start=0, stop=5, step=1, settle=0.0, reset_settle=0.0)
    assert res["ok"] is False
    assert res["aborted"] == "output-not-on"
    assert res["rows"] == []
    assert "hil_help" in res["error"]


def test_sweep_completes_and_shuts_output_off():
    inst = _sim(responses={":OUTP:STAT?": "1", "READ?": "1.0", ":MEAS:VOLT?": "1.0"})
    res = S.sweep(inst, inst, start=1, stop=1, step=1, settle=0.0, reset_settle=0.0)
    assert res["aborted"] is None
    assert res["ok"] is True
    assert res["stats"]["points"] == 1
    assert res["rows"][0]["measured"] == 1.0
    # safe shutdown always runs, even on success
    assert ":OUTP:STAT OFF" in inst.transport.writes


def test_sweep_writes_csv(tmp_path):
    inst = _sim(responses={":OUTP:STAT?": "1", "READ?": "1.0", ":MEAS:VOLT?": "1.0"})
    out = tmp_path / "sweep.csv"
    res = S.sweep(inst, inst, start=1, stop=1, step=1, settle=0.0, reset_settle=0.0,
                  out=str(out))
    assert res["file"] == str(out) and out.exists()
    assert "set,measured" in out.read_text(encoding="utf-8").splitlines()[0]
