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
from ..instrument import Instrument
from ..telemetry import DataLogger, summarize
from .assertions import evaluate
from .procedure import Procedure, Step, _normalize_step
from .report import Measurement, RunReport, StepResult

InstrumentFactory = Callable[[], Instrument]
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
    inst: Instrument,
    variables: dict[str, Any],
    measurements: list[Measurement],
    captures: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    sr: StepResult,
    *,
    coe: bool = False,
    on_step: StepHook | None = None,
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

    elif stype == "capture":
        channel = str(args["channel"])
        seconds = float(args["seconds"])
        interval = args.get("interval")
        units = str(args.get("units", ""))
        samples = inst.capture(
            channel, seconds, float(interval) if interval is not None else None, units
        )
        stats = summarize(samples, units)
        out = args.get("out")
        if out:
            with DataLogger(out) as log:
                log.writemany(samples)
            stats["file"] = str(out)
        captures.append(stats)
        sr.value = stats
        sr.response = f"{stats.get('count', 0)} samples, mean={stats.get('mean')}"
        if args.get("save_as"):
            variables[args["save_as"]] = stats.get("mean")

    elif stype == "logic":
        _run_logic(args, inst, variables, artifacts, sr)

    elif stype == "decode":
        _run_decode(args, inst, variables, artifacts, sr)

    elif stype == "repeat":
        _run_repeat(args, inst, variables, measurements, captures, artifacts, sr,
                    coe=coe, on_step=on_step)

    else:  # pragma: no cover - validation rejects unknown types first
        sr.ok = False
        sr.message = f"unknown step type '{stype}'"


def _logic_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    """Map a ``logic`` step's args (with synonyms) to run_capture() kwargs."""
    kw: dict[str, Any] = {}
    if args.get("channels") is not None:
        kw["channels"] = args["channels"]
    rate = args.get("sample_rate_hz", args.get("rate"))
    if rate is not None:
        kw["sample_rate_hz"] = float(rate)
    depth = args.get("depth_samples", args.get("depth"))
    if depth is not None:
        kw["depth_samples"] = int(float(depth))
    dur = args.get("duration_s", args.get("seconds"))
    if dur is not None:
        kw["duration_s"] = float(dur)
    if args.get("trigger") is not None:
        kw["trigger"] = args["trigger"]
    if args.get("format"):
        kw["fmt"] = str(args["format"])
    if args.get("mode"):
        kw["mode"] = str(args["mode"])
    if args.get("out"):
        kw["out"] = str(args["out"])
    if args.get("timeout") is not None:
        kw["timeout"] = float(args["timeout"])
    return kw


def _run_logic(
    args: dict[str, Any],
    inst: Instrument,
    variables: dict[str, Any],
    artifacts: list[dict[str, Any]],
    sr: StepResult,
) -> None:
    """Run a logic-analyzer capture into a file-backed artifact."""
    run_capture = getattr(inst, "run_capture", None)
    if not callable(run_capture):
        raise HilError(
            "a 'logic' step needs a logic-analyzer instrument (device driver "
            "'logic', address 'logic://host:port'); this run is not on one"
        )
    art = run_capture(**_logic_kwargs(args))
    d = art.to_dict()
    artifacts.append(d)
    sr.value = d
    sr.response = (f"{d.get('sample_count', 0)} samples "
                   f"@{d.get('sample_rate_hz', 0)} Hz -> {d.get('path', '')}")
    if args.get("save_as"):
        variables[args["save_as"]] = d.get("path")


def _run_decode(
    args: dict[str, Any],
    inst: Instrument,
    variables: dict[str, Any],
    artifacts: list[dict[str, Any]],
    sr: StepResult,
) -> None:
    """Decode an artifact's protocol data into a decoded artifact."""
    decode_fn = getattr(inst, "decode", None)
    if not callable(decode_fn):
        raise HilError(
            "a 'decode' step needs a logic-analyzer instrument (device driver 'logic')"
        )
    decs = args.get("decoders")
    if decs is None:
        name = args.get("decoder")
        entry: dict[str, Any] = {"name": name}
        if args.get("options"):
            entry["options"] = args["options"]
        cmap = args.get("map", args.get("channels"))
        if cmap:
            entry["channels"] = cmap
        decs = [entry]
    kwargs: dict[str, Any] = {}
    if args.get("format"):
        kwargs["fmt"] = str(args["format"])
    if args.get("out"):
        kwargs["out"] = str(args["out"])
    src = args.get("input")
    if isinstance(src, str) and src in variables:
        src = variables[src]
    if src is not None:
        kwargs["artifact"] = src
    art = decode_fn(decs, **kwargs)
    d = art.to_dict()
    artifacts.append(d)
    sr.value = d
    sr.response = f"{d.get('frames', 0)} frames -> {d.get('path', '')}"
    if args.get("save_as"):
        variables[args["save_as"]] = d.get("frames", d.get("path"))


def _run_repeat(
    args: dict[str, Any],
    inst: Instrument,
    variables: dict[str, Any],
    measurements: list[Measurement],
    captures: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    sr: StepResult,
    *,
    coe: bool = False,
    on_step: StepHook | None = None,
) -> None:
    """Run a nested ``steps`` body ``count`` times.

    Nested steps stream through ``on_step`` for live progress but are *not*
    appended to the top-level report (which would balloon for an hour-long
    soak); their measurements/captures still accumulate, and the ``repeat``
    step records a compact summary. A nested failure stops the loop unless
    ``continue_on_error`` is set.
    """
    try:
        count = int(str(args.get("count", 0)).strip())
    except (TypeError, ValueError):
        raise HilError(f"repeat: 'count' must be an integer, got {args.get('count')!r}")
    body: list[Step] = []
    for j, raw in enumerate(args.get("steps") or [], 1):
        st, _err = _normalize_step(raw, j)
        if st is not None:
            body.append(st)

    cycles = 0
    failures = 0
    last_msg = ""
    for _ in range(max(0, count)):
        cycle_failed = False
        for st in body:
            sub = _run_step(st, inst, variables, measurements, captures, artifacts,
                            coe=coe, on_step=on_step)
            if on_step is not None:
                on_step(sub)
            if not sub.ok:
                failures += 1
                last_msg = sub.message or ""
                cycle_failed = True
                if not coe:
                    break
        cycles += 1
        if cycle_failed and not coe:
            break

    sr.value = {"count": max(0, count), "cycles": cycles, "failures": failures}
    sr.response = f"{cycles}/{max(0, count)} cycles, {failures} step failures"
    if failures:
        sr.ok = False
        sr.message = last_msg or "a step inside repeat failed"


def _run_step(
    step: Step,
    inst: Instrument,
    variables: dict[str, Any],
    measurements: list[Measurement],
    captures: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    coe: bool = False,
    on_step: StepHook | None = None,
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
        _execute(step.type, args, inst, variables, measurements, captures, artifacts, sr,
                 coe=coe, on_step=on_step)
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
    captures: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
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
                sr = _run_step(s, inst, variables, measurements, captures, artifacts,
                               coe=coe, on_step=on_step)
                if not sr.ok and not coe:
                    halted = True
            results.append(sr)
            if on_step is not None:
                on_step(sr)
    finally:
        try:
            inst.close()
        except Exception:  # noqa: BLE001 - close must never mask a result
            pass

    report.steps = results
    report.measurements = measurements
    report.captures = captures
    report.artifacts = artifacts
    report.duration_s = time.monotonic() - t0
    report.ok = not any((not s.ok and not s.skipped) for s in results)
    return report
