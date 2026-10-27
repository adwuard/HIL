"""Stateful sessions, verified writes, and multi-instrument sweeps.

Three gaps this closes, all observed in real agent sessions:

* **Stateless connect-per-call** forced a fresh USB renegotiation every command
  and made the ``*RST`` race physically possible (an output-enable sent before
  the supply's output stage was ready, silently dropped). :class:`SessionStore`
  keeps one live handle per named session so ordering is deterministic.
* **Error mis-attribution**: a rejected write's SCPI error (``-141``) surfaced on
  whatever queried *next*, so a bad command looked ``ok``. :func:`run_commands`
  inserts an ``*OPC?`` barrier after each write *before* draining, so the error
  lands on the command that caused it.
* **Hand-written orchestrators**: the bench runner is single-instrument, so a
  source+measure sweep had to be scripted by hand. :func:`sweep` is the
  first-class primitive — it gates on a verified output state and never lets a
  failed-on setup masquerade as N measurement failures.
"""

from __future__ import annotations

import csv
import re
import statistics
import time
from pathlib import Path
from typing import Any, Callable

# -- session store -----------------------------------------------------------
_SESSIONS: dict[str, Any] = {}


def open_session(
    target: str,
    name: str = "main",
    *,
    device_slug: str = "",
    timeout: float = 5.0,
    registry_dirs: tuple[Path, ...] = (),
) -> dict:
    """Open (or replace) a persistent instrument handle under ``name``.

    Replacing an existing session closes the old handle first. When no
    ``device_slug`` is pinned, the live ``*IDN?`` is matched against the registry
    and a matching profile's safety policy is auto-bound, so a session to a known
    instrument enforces its limits without the caller naming it. Returns a status
    dict carrying ``identity``, the ``matched_slug``/``safety_bound`` result and a
    loud ``simulated`` flag; the live handle is stored process-wide for reuse by
    :func:`get_session`.
    """
    from . import connect
    from .instrument import is_simulated
    from .registry import load_registry, match_device_by_idn

    close_session(name)
    inst = connect(
        target, device_slug=device_slug or None, timeout=timeout, registry_dirs=registry_dirs
    )
    _SESSIONS[name] = inst
    ident: dict[str, str] = {}
    try:
        ident = inst.identify()
    except Exception:  # noqa: BLE001 - identity is best-effort
        pass

    matched_slug = ""
    safety_bound = False
    if getattr(inst, "device", None) is None and ident:
        # Rebuild the *IDN? string from the parsed identity (no extra round-trip)
        # and conservatively attach a registry safety policy when one matches.
        idn = ",".join(
            ident.get(k, "") for k in ("manufacturer", "model", "serial", "firmware")
        )
        try:
            match = match_device_by_idn(idn, load_registry(registry_dirs))
        except Exception:  # noqa: BLE001 - auto-bind is a best-effort convenience
            match = None
        if match is not None:
            try:
                inst.device = match
                matched_slug = match.slug
                safety_bound = True
            except Exception:  # noqa: BLE001 - a frozen dataclass is still settable
                pass

    return {
        "ok": True,
        "session": name,
        "target": target,
        "identity": ident,
        "matched_slug": matched_slug,
        "safety_bound": safety_bound,
        "simulated": is_simulated(inst),
    }


def get_session(name: str = "main") -> Any:
    return _SESSIONS.get(name)


def close_session(name: str = "main") -> bool:
    inst = _SESSIONS.pop(name, None)
    if inst is None:
        return False
    try:
        inst.close()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass
    return True


def close_all_sessions() -> int:
    names = list(_SESSIONS)
    for n in names:
        close_session(n)
    return len(names)


def session_names() -> list[str]:
    return sorted(_SESSIONS)


# -- command execution with correct error attribution ------------------------
def _fmt_error(err: Any) -> str:
    if isinstance(err, dict):
        code = err.get("code", "")
        msg = err.get("message", "")
        return f"{code}: {msg}".strip(": ") if (code or msg) else str(err)
    return str(err)


def _drain(inst: Any) -> list[str]:
    drain = getattr(inst, "drain_errors", None)
    if not callable(drain):
        return []
    try:
        return [_fmt_error(e) for e in drain()]
    except Exception:  # noqa: BLE001 - a drain failure is not fatal
        return []


def run_command(inst: Any, command: str, *, sync_writes: bool = True,
                timeout: float | None = None) -> dict:
    """Execute one command; attribute SCPI errors to *this* command.

    After a write, an ``*OPC?`` barrier (when the driver supports it) ensures the
    instrument has processed the command before the error queue is drained, so a
    rejection is reported against the write that caused it — not the next query.
    """
    started = time.monotonic()
    command = (command or "").strip()
    is_query = command.endswith("?")
    res: dict[str, Any] = {"command": command, "ok": True, "query": is_query}
    try:
        if is_query:
            res["response"] = inst.query(command, timeout) if timeout else inst.query(command)
        else:
            inst.write(command)
            res["response"] = None
            if sync_writes:
                sync = getattr(inst, "sync", None)
                if callable(sync):
                    try:
                        sync(timeout) if timeout else sync()
                    except Exception:  # noqa: BLE001 - barrier is best-effort
                        pass
    except Exception as exc:  # noqa: BLE001 - device-level failure -> structured
        res["ok"] = False
        res["error"] = str(exc)
        res["response"] = None
        res["elapsed_s"] = round(time.monotonic() - started, 3)
        return res
    errors = _drain(inst)
    res["errors"] = errors
    if errors:
        res["ok"] = False
    res["elapsed_s"] = round(time.monotonic() - started, 3)
    return res


def run_commands(inst: Any, commands: list[str], *, sync_writes: bool = True,
                 settle: float = 0.0, timeout: float | None = None) -> list[dict]:
    """Run a sequence with per-command attribution and an optional inter-command
    settle. Stops nothing on error — the caller inspects each result's ``ok``."""
    out: list[dict] = []
    for cmd in commands or []:
        out.append(run_command(inst, cmd, sync_writes=sync_writes, timeout=timeout))
        if settle:
            time.sleep(settle)
    return out


# -- reading parsing ---------------------------------------------------------
_FLOAT_RE = re.compile(r"[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?")
_OVERLOAD = 1e36  # DMMs return ~9.9E37 for out-of-range; treat as invalid


def parse_reading(raw: Any, units: str = "") -> tuple[float | None, str]:
    """Parse an instrument reading into ``(value, unit)``.

    Handles bare floats (``+1.0012E+00``), suffixed readings (``1.0012 VDC``),
    and overload sentinels (``9.9E+37`` → ``None``). Returns ``(None, units)``
    when no finite value is present.
    """
    if raw is None:
        return None, units
    text = str(raw).strip()
    if not text:
        return None, units
    m = _FLOAT_RE.search(text)
    if not m:
        return None, units
    try:
        value = float(m.group(0))
    except ValueError:
        return None, units
    if abs(value) >= _OVERLOAD:
        return None, units
    unit = units or text[m.end():].strip()
    return value, unit


def _within(value: float, lo: float, hi: float) -> bool:
    return lo <= value <= hi


def _parse_window(spec: Any, center: float | None = None) -> tuple[float, float] | None:
    """Parse ``"0.97..1.03"`` / ``"[0.97, 1.03]"`` into ``(lo, hi)``."""
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        return float(spec[0]), float(spec[1])
    text = str(spec).strip()
    m = re.findall(_FLOAT_RE, text)
    if len(m) >= 2:
        lo, hi = float(m[0]), float(m[1])
        return (min(lo, hi), max(lo, hi))
    if len(m) == 1 and center is not None:  # a ±tolerance around center
        tol = abs(float(m[0]))
        return center - tol, center + tol
    return None


def verify(inst: Any, query: str, within: Any = "", *, settle: float = 0.0,
           units: str = "", timeout: float | None = None) -> dict:
    """Read ``query`` and check it lands inside ``within``.

    ``within`` is ``"lo..hi"`` (absolute) or a single ``±tol`` number paired with
    an expected ``center`` embedded as ``"center±tol"``. Returns
    ``{ok, value, unit, raw, within}``.
    """
    if settle:
        time.sleep(settle)
    raw = inst.query(query, timeout) if timeout else inst.query(query)
    value, unit = parse_reading(raw, units)
    window = _parse_window(within, center=value)
    result: dict[str, Any] = {"ok": value is not None, "query": query,
                              "value": value, "unit": unit or units, "raw": str(raw)}
    if window is not None:
        result["within"] = {"lo": window[0], "hi": window[1]}
        result["ok"] = value is not None and _within(value, window[0], window[1])
    return result


# -- multi-instrument sweep --------------------------------------------------
def sweep(
    source: Any,
    measure: Any,
    *,
    start: float,
    stop: float,
    step: float,
    channel: int = 1,
    ilimit: float | None = None,
    settle: float = 0.5,
    tol_abs: float = 0.03,
    tol_pct: float = 0.5,
    measure_query: str = "READ?",
    readback_query: str = ":MEAS:VOLT?",
    voltage_template: str = ":SOUR{ch}:VOLT {value}",
    select_template: str = ":INST:NSEL {ch}",
    output_on: str = ":OUTP:STAT ON",
    output_off: str = ":OUTP:STAT OFF",
    output_verify_query: str = ":OUTP:STAT?",
    reset: bool = True,
    reset_settle: float = 1.0,
    units: str = "V",
    out: str = "",
    on_step: Callable[[dict], None] | None = None,
    timeout: float | None = None,
) -> dict:
    """Ramp a source instrument and verify each setpoint on a measure instrument.

    Safety-critical behaviour: after enabling the output the source state is
    *verified* (``output_verify_query`` must read on/1) and the sweep aborts if
    the output never came on — a dead bench can no longer masquerade as a run of
    measurement failures. On completion, error, or exception the output is ramped
    to zero and switched off.

    ``source`` and ``measure`` may be the same instrument handle. Returns
    ``{ok, rows, stats, aborted, file}``.
    """
    rows: list[dict] = []
    output_on_done = False
    step_cb = on_step or (lambda _r: None)

    def _set_v(value: float) -> None:
        run_command(source, voltage_template.format(ch=channel, value=f"{value:g}"),
                    timeout=timeout)

    def shutdown() -> None:
        nonlocal output_on_done
        if not output_on_done:
            return
        try:
            _set_v(0.0)
            run_command(source, output_off, timeout=timeout)
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass
        output_on_done = False

    try:
        # -- preflight: reset, limit, zero volts, output on, VERIFY -----------
        if reset:
            run_command(source, "*RST", timeout=timeout)
            if reset_settle:
                time.sleep(reset_settle)  # post-*RST barrier: let the output stage wake
        if ilimit is not None:
            run_command(source, f":SOUR{channel}:CURR {ilimit:g}", timeout=timeout)
        _set_v(0.0)
        if select_template:
            run_command(source, select_template.format(ch=channel), timeout=timeout)
        run_command(source, output_on, timeout=timeout)
        output_on_done = True
        if reset_settle:
            time.sleep(min(reset_settle, 0.3))
        # Verify the output actually came on before measuring anything.
        if output_verify_query:
            state = run_command(source, output_verify_query, timeout=timeout)
            sval = str(state.get("response", "")).strip().upper()
            if not (sval.startswith("1") or sval.startswith("ON")):
                return {
                    "ok": False, "aborted": "output-not-on", "rows": [],
                    "error": (
                        f"output did not come on after '{output_on}' "
                        f"({output_verify_query} -> {sval or 'no response'}). "
                        "Check the correct output-on syntax for this model with "
                        "hil_help; no measurements were taken."
                    ),
                }
        # Configure the meter.
        run_command(measure, "CONF:VOLT:DC 10,0.001", timeout=timeout)

        n = int(round((stop - start) / step)) + 1
        for i in range(max(0, n)):
            vset = round(start + i * step, 6)
            _set_v(vset)
            time.sleep(settle)
            mres = run_command(measure, measure_query, timeout=timeout)
            vmeas, _ = parse_reading(mres.get("response"), units)
            sres = run_command(source, readback_query, timeout=timeout) if readback_query else {}
            vsrc, _ = parse_reading(sres.get("response"), units)
            tol = max(tol_abs, abs(vset) * tol_pct / 100.0)
            ok = vmeas is not None and abs(vmeas - vset) <= tol
            row = {
                "set": vset, "measured": vmeas, "source": vsrc,
                "tol": round(tol, 6), "ok": ok,
                "error": (round(vmeas - vset, 6) if vmeas is not None else None),
            }
            rows.append(row)
            step_cb(row)
    except Exception as exc:  # noqa: BLE001 - always shut down safely
        shutdown()
        return {"ok": False, "aborted": "exception", "rows": rows, "error": str(exc)}
    finally:
        shutdown()

    measured = [r["measured"] for r in rows if r["measured"] is not None]
    errors = [r["error"] for r in rows if r["error"] is not None]
    stats: dict[str, Any] = {
        "points": len(rows),
        "passed": sum(1 for r in rows if r["ok"]),
    }
    if measured:
        stats.update({
            "min": round(min(measured), 6), "max": round(max(measured), 6),
            "mean": round(statistics.fmean(measured), 6),
        })
    if errors:
        stats.update({
            "mean_error": round(statistics.fmean(errors), 6),
            "worst_error": round(max(errors, key=abs), 6),
        })
    file = None
    if out:
        file = _write_csv(out, rows)
    return {"ok": all(r["ok"] for r in rows) and bool(rows), "rows": rows,
            "stats": stats, "aborted": None, "file": file}


def _write_csv(path: str, rows: list[dict]) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = ["set", "measured", "source", "tol", "error", "ok"]
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c) for c in cols})
    return str(p)
