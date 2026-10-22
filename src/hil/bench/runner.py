"""Execute a bench procedure against a simulated or real instrument.

:func:`run_procedure` drives an :class:`~hil.scpi.ScpiInstrument` (built by a
caller-supplied factory, so the same engine runs on the simulator, a fake
socket, or real hardware) through the procedure's steps, capturing responses,
measurements and assertion outcomes into a :class:`~hil.bench.report.RunReport`.

Design points:

* **Variables** — ``${name}`` is substituted into any string arg; ``query``,
  ``measure``, ``idn`` and ``set`` steps write back into the variable scope.
* **Safety** — commands go through ``ScpiInstrument.write/query``, so the
  device policy is enforced even in a dry-run; a blocked command fails the step.
* **Failure handling** — by default the first failing step halts the run and the
  rest are marked skipped; ``continue_on_error`` runs everything.
* **Never crashes on SCPI-level errors** — transport/instrument/safety errors
  become step failures with an actionable message.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..errors import HilError
from ..scpi import ScpiInstrument
from .assertions import evaluate
from .procedure import Procedure, Step
from .report import Measurement, RunReport, StepResult

InstrumentFactory = Callable[[], ScpiInstrument]
StepHook = Callable[[StepResult], None]

_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    """Replace ``${name}`` in strings, recursing into dicts and lists."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            return str(variables[name]) if name in variables else m.group(0)
        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    return value


def _resolve_that(that: Any, variables: dict[str, Any]) -> Any:
    """Resolve an ``assert.that`` operand: variable name, ``${...}``, or literal."""
    if isinstance(that, str):
        s = that.strip()
        if s in variables:
            return variables[s]
        if _VAR_RE.search(s):
            return _substitute(s, variables)
    return that


def _parse_value(response: str, parse: str) -> Any:
    text = (response or "").strip()
    parse = (parse or "float").lower()
    if parse in ("str", "raw", "string"):
        return text
    try:
        num = float(text)
    except (TypeError, ValueError):
        return text
    return int(num) if parse in ("int", "integer") else num


def _execute(
    stype: str,
    args: dict[str, Any],
    inst: ScpiInstrument,
    variables: dict[str, Any],
    measurements: list[Measurement],
    sr: StepResult,
) -> None:
    """Run one step, mutating ``sr`` / ``variables`` / ``measurements``."""
    if stype == "reset":
        inst.reset()

    elif stype == "opc":
        done = inst.opc()
        sr.value = done
        sr.response = "1" if done else "0"
        if args.get("save_as"):
            variables[args["save_as"]] = done

    elif stype == "idn":
        raw = inst.idn()
        sr.response = raw
        sr.value = inst.identify()
        if args.get("save_as"):
            variables[args["save_as"]] = raw

    elif stype == "write":
        inst.write(str(args["command"]))

    elif stype == "query":
        resp = inst.query(str(args["command"]))
        sr.response = resp
        sr.value = resp
        if args.get("save_as"):
            variables[args["save_as"]] = resp

    elif stype == "measure":
        cmd = str(args["command"])
        resp = inst.query(cmd)
        value = _parse_value(resp, str(args.get("parse", "float")))
        sr.response = resp
        sr.value = value
        name = str(args.get("save_as") or cmd)
        if args.get("save_as"):
            variables[args["save_as"]] = value
        measurements.append(
            Measurement(name=name, command=cmd, value=value, units=str(args.get("units", "")))
        )

    elif stype == "assert":
        actual = _resolve_that(args.get("that"), variables)
        ok, detail = evaluate(str(args.get("op", "")), actual, args.get("value"))
        sr.ok = ok
        sr.value = actual
        user_msg = str(args.get("message", ""))
        sr.message = f"{user_msg} — {detail}" if user_msg else detail

    elif stype == "errors":
        errs = inst.drain_errors()
        sr.errors = errs
        sr.value = len(errs)
        expect = str(args.get("expect", "any")).lower()
        if args.get("save_as"):
            variables[args["save_as"]] = errs
        if expect == "none":
            sr.ok = not errs
            if errs:
                sr.message = f"expected no errors, got {errs}"
        elif expect == "any":
            sr.ok = bool(errs)
            if not errs:
                sr.message = "expected an error, but the queue was empty"

    elif stype == "delay":
        seconds = float(args["seconds"])
        time.sleep(seconds)
        sr.value = seconds

    elif stype == "wait":
        inst.wait_until_done(timeout=float(args.get("timeout", 30.0)))

    elif stype == "set":
        variables[str(args["var"])] = args["value"]
        sr.value = args["value"]

    elif stype == "log":
        sr.message = str(args.get("message", ""))

    else:  # pragma: no cover - validation rejects unknown types first
        sr.ok = False
        sr.message = f"unknown step type '{stype}'"


def _run_step(
    step: Step,
    inst: ScpiInstrument,
    variables: dict[str, Any],
    measurements: list[Measurement],
) -> StepResult:
    args = _substitute(step.args, variables)
    sr = StepResult(
        index=step.index,
        type=step.type,
        summary=Step(type=step.type, args=args, index=step.index).summary(),
        ok=True,
    )
    started = time.monotonic()
    try:
        _execute(step.type, args, inst, variables, measurements, sr)
    except HilError as exc:
        sr.ok = False
        sr.message = str(exc)
    except Exception as exc:  # noqa: BLE001 - a step failure is data, not a crash
        sr.ok = False
        sr.message = f"{type(exc).__name__}: {exc}"
    sr.elapsed_s = round(time.monotonic() - started, 4)
    return sr


def run_procedure(
    proc: Procedure,
    make_instrument: InstrumentFactory,
    *,
    mode: str = "sim",
    target: str = "simulator",
    continue_on_error: bool | None = None,
    on_step: StepHook | None = None,
) -> RunReport:
    """Run ``proc`` and return a :class:`RunReport`. Never raises for run-level
    failures (a bad connection becomes a failed report)."""
    coe = proc.continue_on_error if continue_on_error is None else continue_on_error
    variables = dict(proc.variables)
    measurements: list[Measurement] = []
    results: list[StepResult] = []
    t0 = time.monotonic()
    report = RunReport(
        name=proc.name,
        ok=True,
        mode=mode,
        target=target,
        device=proc.device,
        description=proc.description,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    try:
        inst = make_instrument()
    except Exception as exc:  # noqa: BLE001 - connection failure -> failed report
        connect_sr = StepResult(
            index=0, type="connect", summary=f"connect {target}", ok=False,
            message=f"{type(exc).__name__}: {exc}",
        )
        results.append(connect_sr)
        if on_step is not None:
            on_step(connect_sr)
        for s in proc.steps:
            results.append(
                StepResult(index=s.index, type=s.type, summary=s.summary(),
                           ok=False, skipped=True)
            )
        report.steps = results
        report.ok = False
        report.duration_s = time.monotonic() - t0
        return report

    halted = False
    try:
        for s in proc.steps:
            if halted:
                sr = StepResult(index=s.index, type=s.type, summary=s.summary(),
                                ok=False, skipped=True)
            else:
                sr = _run_step(s, inst, variables, measurements)
                if not sr.ok and not coe:
                    halted = True
            results.append(sr)
            if on_step is not None:
                on_step(sr)
    finally:
        try:
            inst.transport.close()
        except Exception:  # noqa: BLE001 - close must never mask a result
            pass

    report.steps = results
    report.measurements = measurements
    report.duration_s = time.monotonic() - t0
    report.ok = not any((not s.ok and not s.skipped) for s in results)
    return report
