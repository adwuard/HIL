"""HIL MCP server — HIL's bench primitives as native Model Context Protocol tools.

This exposes the *same* capabilities the ``hil`` CLI does — instrument control,
on-chip flash/debug, the device registry, and backend provisioning — as MCP tools
and resources that coding agents (Claude Code, Codex CLI, Cursor, Windsurf, Zed,
VS Code, Cline, …) can call directly.

**Local-only by design.** The server runs over **stdio** (the transport every
local coding agent speaks) or loopback streamable-HTTP, and drives the instruments
and debug probes attached to *this* host. Nothing is proxied to the cloud, and no
port is exposed beyond 127.0.0.1.

Launch (this is what an agent spawns)::

    hil mcp                              # stdio server (default)
    hil mcp --transport http --port 8765 # loopback streamable-HTTP

Make it globally available to your agents::

    hil mcp install                      # merge into detected agents' user config
    hil mcp install --agent codex        # one agent
    hil mcp install --dry-run            # preview, write nothing
    hil mcp config                       # print the snippet to paste manually

Requires the ``mcp`` SDK::

    pip install 'hil-harness[mcp]'       # or: uv pip install 'mcp>=1.2'

Every tool returns a JSON object with an ``ok`` flag; on failure ``ok`` is false
and ``error`` explains why (mirroring :meth:`~hil.instrument.BaseInstrument.safe_execute`),
so an agent always gets a structured result rather than a transport-level error.
"""

from __future__ import annotations

import functools
import json
import os
import platform as _platform
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__, connect, load_registry
from .errors import (
    AddressError,
    DeviceNotFoundError,
    HilError,
    InstrumentError,
    SafetyViolationError,
    TimeoutError_,
    TransportError,
    TransportNotFoundError,
)

# --- MCP SDK shim -----------------------------------------------------------
# mcp 2.x renamed FastMCP -> MCPServer (same .tool()/.resource()/.run() API).
# Support both so the server works whichever the host has installed.
_Server: Any = None
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server  # type: ignore[assignment]
except ImportError:  # pragma: no cover - exercised only on mcp 1.x
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]
    except ImportError:  # pragma: no cover - mcp not installed at all
        _Server = None

SERVER_NAME = "hil"

INSTRUCTIONS = (
    "HIL — Hardware-in-the-Loop bench control for the instruments and on-chip debug "
    "probes attached to THIS host.\n\n"
    "RECOMMENDED WORKFLOW (this order is what makes a cold bench succeed):\n"
    "1. hil_discover — find what is ACTUALLY plugged in (returns each device's "
    "address, *IDN?, and matched model). Do NOT guess addresses or fall back to "
    "shell tools; this one call enumerates USB-TMC/GPIB, serial, LAN, and debug "
    "probes. hil_doctor tells you which backends still need installing.\n"
    "2. hil_help — look up the CORRECT SCPI mnemonic and parameters for the exact "
    "model BEFORE sending anything (e.g. hil_help(model='DP932E', command='OUTP:STAT')). "
    "Never guess SCPI syntax; the corpus covers 725 instruments and short/long forms "
    "and optional brackets are matched for you.\n"
    "3. hil_exec — run the looked-up commands. For a sequence that must keep state "
    "(a selected channel, an enabled output), open a hil_session first and pass "
    "session=; otherwise each call connects/closes.\n"
    "4. hil_verify / hil_sweep — confirm a readback lands in a window, or ramp a "
    "source and verify each setpoint. hil_sweep verifies the output actually came "
    "on and aborts safely if it did not, so a dead bench never looks like success.\n\n"
    "Also: hil_devices/hil_device browse the static registry catalogue and safety "
    "policy; hil_probe confirms one address is alive; hil_capture samples a channel "
    "over time; hil_analyze summarizes a saved capture; hil_scrape fetches a reference "
    "on demand when hil_help says a model is outside the corpus; hil_debug_probe/"
    "hil_debug flash/halt/step/read/write a Cortex-M or ESP target; hil_setup plans "
    "backend provisioning.\n\n"
    "Targets are addresses (tcp://host:5025, serial://…, visa://GPIB::24::INSTR, "
    "visa://USB0::…), device slugs, or debug URLs "
    "(debug://probe-rs?target=nRF52832_xxAA&transport=swd). Every tool returns "
    "{ok, …}; on failure ok=false with a machine-readable 'code', an 'error' message, "
    "and a 'hint' saying what to do next."
)


# --- small helpers ----------------------------------------------------------
def _require_sdk() -> Any:
    """Return the MCP server class, or raise an actionable install hint."""
    if _Server is None:  # pragma: no cover - depends on host env
        raise HilError(
            "the MCP server needs the 'mcp' SDK — install it with: "
            "pip install 'hil-harness[mcp]'  (or: uv pip install 'mcp>=1.2')"
        )
    return _Server


# Machine-readable error classification. An agent must be able to tell "retry"
# from "install a backend" from "wrong SCPI syntax" without parsing prose, so
# every failure carries a stable `code` plus an actionable `hint`.
_SCPI_CODE_RE = re.compile(r"(-?\d{2,4})\s*,")


def _classify(exc: Exception) -> tuple[str, str]:
    """Map an exception to a ``(code, hint)`` pair for the error envelope."""
    msg = str(exc)
    if isinstance(exc, TransportNotFoundError):
        return (
            "backend-missing",
            "a transport backend is not installed — run `hil doctor`, then the "
            "`hil setup <slug>` / uv pip install command it prints.",
        )
    if isinstance(exc, SafetyViolationError):
        return ("safety-blocked", "the device safety policy rejected this command; "
                "check the profile's blocked/allow list via hil_device.")
    if isinstance(exc, AddressError):
        return ("bad-address", "use tcp://host:5025, serial://PORT, visa://RESOURCE, "
                "debug://backend, or a registry slug; find live ones with hil_discover.")
    if isinstance(exc, DeviceNotFoundError):
        return ("unknown-device", "the slug is not in the registry; list slugs with "
                "hil_devices or match live hardware with hil_discover.")
    if isinstance(exc, TimeoutError_):
        return ("timeout", "the instrument did not respond in time; raise `timeout` "
                "or check the address/cabling.")
    if isinstance(exc, InstrumentError):
        m = _SCPI_CODE_RE.search(msg)
        code = f"scpi:{m.group(1)}" if m else "scpi-error"
        return (code, "the instrument rejected a command; look up the correct "
                "syntax for this exact model with hil_help.")
    if isinstance(exc, TransportError):
        return ("no-instrument", "could not open/read the instrument; confirm it is "
                "powered, connected, and reachable (hil_discover).")
    if isinstance(exc, HilError):
        return ("hil-error", msg.splitlines()[0][:200] if msg else "")
    low = msg.lower()
    if "not installed" in low or "no module named" in low or "requires the" in low:
        return ("backend-missing", "a required library is missing — run `hil doctor` "
                "for the exact install command.")
    return (f"{type(exc).__name__}", "")


def _safe(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn any raised error into ``{"ok": False, "error": …, "code": …, "hint": …}``.

    Agents get one consistent structured shape from every tool (the same contract
    as :meth:`~hil.instrument.BaseInstrument.safe_execute`) instead of an MCP
    transport error. The ``code`` is machine-matchable (``backend-missing``,
    ``scpi:-141``, ``timeout``, ``safety-blocked``, …) and ``hint`` says what to
    do next. ``functools.wraps`` preserves the name/signature/docstring so the
    SDK still generates the correct tool schema.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface any backend failure
            code, hint = _classify(exc)
            envelope = {"ok": False, "code": code, "error": str(exc)}
            if isinstance(exc, HilError):
                envelope["error"] = str(exc)
            else:
                envelope["error"] = f"{type(exc).__name__}: {exc}"
            if hint:
                envelope["hint"] = hint
            return envelope

    return wrapper


@contextmanager
def _instrument(target: str, device_slug: str = "", timeout: float = 5.0) -> Iterator[Any]:
    """Open an instrument for one stateless tool call and always close it.

    MCP tools are stateless: each call connects, acts, and closes, exactly like a
    ``hil`` CLI invocation, so no handles leak between agent turns.
    """
    inst = connect(target, device_slug=device_slug or None, timeout=timeout)
    try:
        yield inst
    finally:
        try:
            inst.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


# --- tools: environment & registry -----------------------------------------
@_safe
def hil_doctor() -> dict:
    """Report the bench environment: HIL version, Python/platform, which optional
    backends are importable, the running interpreter and whether it has pip/uv, how
    many devices are registered, and which backends still need provisioning — each
    with the EXACT install command for this interpreter (uv-tool/uv venvs have no
    pip, so a plain `pip install` fails; this prints the command that works). Call
    this first to see what hardware paths are ready."""
    from . import provision, pyinstall
    from .transports import backend_details, backend_status

    devices = load_registry()
    prov = provision.status(platform=provision.detect())
    return {
        "ok": True,
        "hil_version": __version__,
        "python": _platform.python_version(),
        "platform": _platform.platform(),
        "backends": backend_status(),
        "backend_details": backend_details(),
        "environment": pyinstall.environment_report(),
        "registry_devices": len(devices),
        "provisioning": [
            {
                "slug": r.recipe.slug,
                "ready": r.satisfied,
                "license": r.recipe.license,
                "hint": "" if r.satisfied else provision.setup_hint(r.recipe.slug),
            }
            for r in prov
        ],
    }


@_safe
def hil_devices(search: str = "", category: str = "", limit: int = 200) -> dict:
    """List or search the device registry (instruments and DUT profiles).

    `search` is a case-insensitive substring matched across slug/manufacturer/
    model/category; `category` filters to one exact category; `limit` bounds the
    result. Each match returns slug, manufacturer, model, category, driver and
    interfaces. Use a slug with hil_device for the full profile."""
    devices = load_registry()
    q = (search or "").strip().lower()
    cat = (category or "").strip().lower()
    out: list[dict] = []
    for d in devices.values():
        if cat and (d.category or "").lower() != cat:
            continue
        if q and not (
            q in d.slug.lower()
            or q in (d.manufacturer or "").lower()
            or q in (d.model or "").lower()
            or q in (d.category or "").lower()
        ):
            continue
        out.append(
            {
                "slug": d.slug,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "category": d.category,
                "driver": getattr(d, "driver", "scpi"),
                "interfaces": list(d.interfaces),
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return {"ok": True, "count": len(out), "devices": out}


@_safe
def hil_device(slug: str) -> dict:
    """Full registry profile for one device slug: capabilities, interfaces,
    connection examples, default address, the curated command set, and the safety
    policy (blocked / allow / limits) that agent-issued commands must respect."""
    from dataclasses import asdict

    devices = load_registry()
    d = devices.get(slug)
    if d is None:
        raise HilError(
            f"Unknown device slug '{slug}'. Search with hil_devices(search=...)."
        )
    data = asdict(d)
    data["commands"] = [asdict(c) for c in d.commands]
    data["safety"] = asdict(d.safety)
    return {"ok": True, "device": data}


@_safe
def hil_help(
    model: str = "",
    target: str = "",
    command: str = "",
    subsystem: str = "",
    limit: int = 40,
    timeout: float = 5.0,
) -> dict:
    """Look up the CORRECT SCPI syntax for an exact instrument model from the
    bundled reference corpus (725 instruments, ~294k commands). This is the tool
    to call BEFORE guessing a command — it answers "what is the real mnemonic /
    parameters for :OUTP:STAT on a Rigol DP932E?" instead of trial-and-error.

    Give EITHER `model` (a model like "DP932E", a slug, or a full *IDN? string)
    OR `target` (a live address/slug — its *IDN? is read and matched for you).
    Then narrow with `command` (a mnemonic fragment, e.g. "OUTP:STAT") and/or
    `subsystem` (e.g. "MEASure"). SCPI short/long forms and optional brackets are
    matched, so "OUTP:STAT" finds ":OUTPut[:STATe]".

    Returns {ok, matched, commands:[{mnemonic, query, parameters, ...}],
    commands_available, count, hint}. `ok` is false (with a hint) when the model
    is outside the corpus — that is a normal, reportable outcome, not a crash."""
    from . import reference

    lookup = (model or "").strip()
    if not lookup and target:
        with _instrument(target, "", timeout) as inst:
            lookup = inst.idn() or ""
    if not lookup:
        raise HilError("hil_help: pass a 'model' (e.g. 'DP932E') or a live 'target' "
                       "address whose *IDN? will be matched.")
    return reference.help_lookup(lookup, subsystem=subsystem, command=command, limit=limit)


@_safe
def hil_discover(
    interfaces: list[str] | None = None,
    identify: bool = True,
    timeout: float = 3.0,
) -> dict:
    """Enumerate the instruments ACTUALLY attached to this host right now.

    `hil_devices` lists the static registry catalogue; this answers "what is
    plugged in?" — the single call that replaces falling back to ioreg/lsusb/arp/
    nc or an inline pyvisa script. `interfaces` is any subset of usb (VISA/USB-TMC/
    GPIB), serial (RS-232), lan (mDNS LXI/VXI-11), debug (on-chip probes); omit it
    for all. With `identify` (default) each candidate gets a short *IDN? round-trip
    and is matched to the reference corpus.

    Returns {ok, count, devices:[{address, transport, backend, idn?, identity?,
    matched_slug?, matched_name?}], backends, warnings}. Every backend is optional
    and every probe best-effort: a missing library or an instrument that refuses
    *IDN? becomes a `warnings` entry with an install hint, never a failure. Feed a
    returned `address` straight back into hil_probe/hil_exec."""
    from . import discovery

    want = tuple(interfaces) if interfaces else discovery.DEFAULT_INTERFACES
    return discovery.discover(want, identify=identify, timeout=timeout)


# --- tools: instrument control ---------------------------------------------
@_safe
def hil_probe(target: str, timeout: float = 5.0) -> dict:
    """Connectivity check for an instrument: open it, read *IDN? and *OPC?, and
    return the parsed identity (manufacturer/model/serial/firmware), the registry
    profile it matches (`matched_slug`, if any), a loud `simulated` flag, plus `ok`.
    `target` is an address (tcp://host:5025, serial://…, visa://GPIB::24::INSTR)
    or a device slug. This is the fastest way to confirm a bench link is alive."""
    from .instrument import is_simulated
    from .registry import load_registry, match_device_by_idn

    with _instrument(target, "", timeout) as inst:
        identity = inst.identify()
        alive = bool(inst.opc())
        simulated = is_simulated(inst)
    idn = ",".join(identity.get(k, "") for k in ("manufacturer", "model", "serial", "firmware"))
    match = match_device_by_idn(idn, load_registry())
    return {"ok": alive, "target": target, "identity": identity,
            "matched_slug": match.slug if match else "", "simulated": simulated}


@_safe
def hil_exec(
    target: str = "",
    commands: list[str] | None = None,
    device_slug: str = "",
    timeout: float = 5.0,
    session: str = "",
    settle: float = 0.0,
    sync_writes: bool = True,
) -> dict:
    """Run one or more commands against an instrument.

    A command ending in '?' is a query (its response is returned); otherwise it is
    a write. When `device_slug` is given, that profile's safety policy is enforced
    (blocked/allow/limits).

    `session`: reuse a persistent handle opened by hil_session instead of
    connect-per-call — deterministic ordering, no USB renegotiation, and state
    (e.g. a selected channel) survives between calls. With no `session`, `target`
    is required and the instrument is opened and closed for this one call.

    `sync_writes` (default true) inserts an *OPC? barrier after each write before
    draining SYST:ERR?, so a rejected command's error is attributed to *that*
    command instead of leaking onto the next one. `settle` sleeps that many seconds
    between commands.

    Returns {ok, results:[{command, ok, query, response, errors, elapsed_s}]}.
    """
    from . import session as _sess
    from .instrument import is_simulated

    cmds = list(commands or [])
    handle = _sess.get_session(session) if session else None
    if handle is not None:
        results = _sess.run_commands(handle, cmds, sync_writes=sync_writes,
                                     settle=settle, timeout=timeout)
        ok = all(bool(r.get("ok", True)) for r in results)
        return {"ok": ok, "session": session, "simulated": is_simulated(handle),
                "results": results}
    if session:
        raise HilError(
            f"no such session '{session}'. Open one with hil_session(action='open', "
            f"name='{session}', target=...); active: {', '.join(_sess.session_names()) or '(none)'}"
        )
    if not target:
        raise HilError("hil_exec: pass a 'target' (address/slug) or an open 'session'.")
    with _instrument(target, device_slug, timeout) as inst:
        results = _sess.run_commands(inst, cmds, sync_writes=sync_writes,
                                     settle=settle, timeout=timeout)
        simulated = is_simulated(inst)
    ok = all(bool(r.get("ok", True)) for r in results)
    return {"ok": ok, "simulated": simulated, "results": results}


@_safe
def hil_errors(target: str, device_slug: str = "", timeout: float = 5.0) -> dict:
    """Drain an instrument's error queue (SCPI ``SYST:ERR?``) and return the list
    of pending device errors. `ok` is true only when the queue is empty."""
    with _instrument(target, device_slug, timeout) as inst:
        errors = inst.drain_errors()
    return {"ok": not errors, "errors": errors}


@_safe
def hil_capture(
    target: str,
    channel: str,
    seconds: float,
    interval: float = 0.0,
    units: str = "",
    setup: list[str] | None = None,
    device_slug: str = "",
    out: str = "",
    timeout: float = 5.0,
) -> dict:
    """Sample `channel` (a query such as ':MEAS:CURR?') for `seconds` and return
    summary statistics (count/mean/min/max/p95/rate). Optional `setup` commands run
    first (e.g. configure the instrument); `interval` sets the poll period (streaming
    devices sample natively); if `out` is given the raw samples are written there as
    NDJSON. Returns {ok, channel, captured, file, stats}."""
    from .instrument import is_simulated
    from .telemetry import DataLogger, summarize

    with _instrument(target, device_slug, timeout) as inst:
        for cmd in setup or []:
            r = inst.safe_execute(cmd)
            if not r.get("ok", True):
                raise HilError(
                    f"setup command failed: {cmd!r} -> "
                    f"{r.get('error') or '; '.join(r.get('errors') or []) or 'error'}"
                )
        samples = inst.capture(channel, seconds, interval or None, units)
        stats = summarize(samples, units)
        simulated = is_simulated(inst)
        path = None
        if out:
            with DataLogger(out) as log:
                log.writemany(samples)
            path = str(out)
    return {"ok": True, "channel": channel, "captured": len(samples), "file": path,
            "simulated": simulated, "stats": stats}


@_safe
def hil_session(
    action: str = "list",
    name: str = "main",
    target: str = "",
    device_slug: str = "",
    timeout: float = 5.0,
) -> dict:
    """Manage a persistent instrument handle so state survives across calls.

    hil_exec/hil_verify/hil_sweep reconnect per call by default; a session keeps one
    live link so ordering is deterministic, USB is not renegotiated every command,
    and a selected channel or output state persists between agent turns.

    `action`: 'open' (connect `target` under `name`, enforcing `device_slug`'s safety
    policy), 'close' (drop `name`), or 'list' (names of all live sessions). Pass the
    resulting `name` as `session=` to hil_exec. Returns {ok, session, identity?,
    sessions?}."""
    from . import session as _sess

    act = (action or "list").strip().lower()
    if act == "open":
        if not target:
            raise HilError("hil_session open: 'target' (address/slug) is required.")
        return _sess.open_session(target, name, device_slug=device_slug, timeout=timeout)
    if act == "close":
        closed = _sess.close_session(name)
        return {"ok": True, "session": name, "closed": closed,
                "sessions": _sess.session_names()}
    if act == "close-all":
        return {"ok": True, "closed": _sess.close_all_sessions(), "sessions": []}
    if act == "list":
        return {"ok": True, "sessions": _sess.session_names()}
    raise HilError(f"hil_session: unknown action '{action}'. Use open, close, "
                   "close-all, or list.")


@_safe
def hil_verify(
    query: str,
    within: str = "",
    target: str = "",
    session: str = "",
    device_slug: str = "",
    units: str = "",
    settle: float = 0.0,
    timeout: float = 5.0,
) -> dict:
    """Read one query and check the value lands inside a window — the missing
    primitive that turns a silent, unverified readback into a pass/fail.

    `within` is "lo..hi" (absolute) or "center±tol" (e.g. "5±0.05"). `query` runs
    against a live `session` if named, else `target` is opened for this one call.
    Returns {ok, value, unit, raw, within}. `ok` is false when the value is outside
    the window or unparseable (e.g. a DMM overload)."""
    from . import session as _sess
    from .instrument import is_simulated

    handle = _sess.get_session(session) if session else None
    if handle is None:
        if session:
            raise HilError(f"no such session '{session}'. Open one with hil_session "
                           f"(action='open'); active: {', '.join(_sess.session_names()) or '(none)'}")
        if not target:
            raise HilError("hil_verify: pass a 'target' (address/slug) or an open 'session'.")
        with _instrument(target, device_slug, timeout) as inst:
            res = _sess.verify(inst, query, within, settle=settle, units=units, timeout=timeout)
            res["simulated"] = is_simulated(inst)
            return res
    res = _sess.verify(handle, query, within, settle=settle, units=units, timeout=timeout)
    res["simulated"] = is_simulated(handle)
    return res


@_safe
def hil_sweep(
    start: float,
    stop: float,
    step: float,
    source: str = "",
    measure: str = "",
    source_session: str = "",
    measure_session: str = "",
    device_slug: str = "",
    channel: int = 1,
    ilimit: float | None = None,
    settle: float = 0.5,
    tol_abs: float = 0.03,
    tol_pct: float = 0.5,
    units: str = "V",
    measure_query: str = "READ?",
    readback_query: str = ":MEAS:VOLT?",
    voltage_template: str = ":SOUR{ch}:VOLT {value}",
    select_template: str = ":INST:NSEL {ch}",
    output_on: str = ":OUTP:STAT ON",
    output_off: str = ":OUTP:STAT OFF",
    output_verify_query: str = ":OUTP:STAT?",
    reset: bool = True,
    reset_settle: float = 1.0,
    out: str = "",
    timeout: float = 10.0,
) -> dict:
    """Ramp a source instrument from `start` to `stop` by `step` and verify each
    setpoint on a measure instrument — the first-class source+measure sweep.

    Safety-critical: after enabling the output the source state is VERIFIED
    (`output_verify_query` must read on/1) and the sweep ABORTS with ok=false and
    zero rows if the output never came on, so a dead bench can no longer masquerade
    as a run of measurement failures. On completion, error, or exception the output
    is ramped to zero and switched off. A post-*RST settle barrier lets the output
    stage wake before the first setpoint.

    `source`/`measure` are addresses or slugs (they may be the same instrument), or
    pass live `source_session`/`measure_session` handles. Look up the correct SCPI
    templates for the exact model with hil_help first. Returns {ok, rows, stats,
    aborted, file}."""
    from . import session as _sess
    from .instrument import is_simulated

    src_inst = _sess.get_session(source_session) if source_session else None
    mea_inst = _sess.get_session(measure_session) if measure_session else None
    if source_session and src_inst is None:
        raise HilError(f"no such session '{source_session}'.")
    if measure_session and mea_inst is None:
        raise HilError(f"no such session '{measure_session}'.")

    owned: list[Any] = []
    try:
        if src_inst is None:
            if not source:
                raise HilError("hil_sweep: pass a 'source' address/slug or 'source_session'.")
            src_inst = connect(source, device_slug=device_slug or None, timeout=timeout)
            owned.append(src_inst)
        if mea_inst is None:
            # No measure session: use an explicit measure target, else fall back to
            # the source handle (single-instrument source+measure, e.g. a SMU).
            if measure and measure != source:
                mea_inst = connect(measure, timeout=timeout)
                owned.append(mea_inst)
            else:
                mea_inst = src_inst
        result = _sess.sweep(
            src_inst, mea_inst, start=start, stop=stop, step=step, channel=channel,
            ilimit=ilimit, settle=settle, tol_abs=tol_abs, tol_pct=tol_pct,
            measure_query=measure_query, readback_query=readback_query,
            voltage_template=voltage_template, select_template=select_template,
            output_on=output_on, output_off=output_off,
            output_verify_query=output_verify_query, reset=reset,
            reset_settle=reset_settle, units=units, out=out, timeout=timeout,
        )
        result["simulated"] = is_simulated(src_inst) or is_simulated(mea_inst)
        return result
    finally:
        for inst in owned:
            try:
                inst.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


@_safe
def hil_analyze(file: str) -> dict:
    """Summarize a previously captured NDJSON sample file (written by hil_capture's
    `out`) into per-channel statistics — no re-capture, no live instrument needed.
    Returns {ok, file, count, channels:{name:{count,mean,min,max,p95,rate_hz,
    units,...}}}."""
    from .telemetry import iter_channels, read_samples, summarize

    samples = read_samples(file)
    groups = list(iter_channels(samples))
    return {
        "ok": True,
        "file": file,
        "count": len(samples),
        "channels": {ch: summarize(ss) for ch, ss in groups},
    }


@_safe
def hil_scrape(
    slug: str = "",
    action: str = "device",
    out: str = "",
    fmt: str = "json",
    timeout: float = 60.0,
) -> dict:
    """Fetch an instrument's SCPI reference from the vendor site ON DEMAND — use it
    when hil_help reports a model is outside the bundled corpus, then hil_help
    resolves it. `action` is 'device' (needs `slug`) or 'catalog' (refresh the
    model→slug index). Artefacts are written under `out`, which defaults to
    ``~/.cache/hil/reference/v1`` — a corpus root hil_help searches — so a scraped
    device is immediately lookable-up. Requires network access.

    Returns {ok, action, slug?, command_count?, stats?, out, next?}."""
    from .scraper import Fetcher, run_catalog, run_device

    out_dir = Path(out) if out else Path.home() / ".cache" / "hil" / "reference" / "v1"
    fetcher = Fetcher(cache_dir=None, timeout=timeout)
    act = (action or "device").strip().lower()
    if act == "catalog":
        report = run_catalog(out_dir, fetcher)
        return {
            "ok": True, "action": "catalog", "count": len(report.catalog),
            "catalog_path": str(report.catalog_path), "requests": fetcher.requests_made,
        }
    if not slug:
        raise HilError("hil_scrape device: 'slug' is required (e.g. 'rigol-dp932e').")
    report = run_device(out_dir, fetcher, slug, fmt=fmt)
    if not report.devices:
        raise HilError(f"hil_scrape: no device scraped for '{slug}'.")
    dev = report.devices[0]
    return {
        "ok": True, "action": "device", "slug": dev.slug, "name": dev.name,
        "manufacturer": dev.manufacturer, "model": dev.model, "category": dev.category,
        "url": dev.url, "stats": dev.stats(), "command_count": dev.command_count,
        "subsystem_count": dev.subsystem_count, "out": str(out_dir),
        "next": f"hil_help(model='{dev.model or slug}') now resolves from {out_dir}",
    }


# --- tools: on-chip debug & flash ------------------------------------------
@_safe
def hil_debug_probe(backend: str = "probe-rs") -> dict:
    """List the on-chip debug probes attached to THIS host (no target required).

    Each probe is parsed into name/type/vid/pid/serial/interface so you can tell
    debuggers apart and pick one by serial. `backend` is probe-rs (default),
    openocd, pyocd, esptool or stlink. Returns {ok, backend, count, probes}."""
    with _instrument(f"debug://{backend}", "", 15.0) as inst:
        probes = inst.probe_list()
    return {"ok": True, "backend": backend, "count": len(probes), "probes": probes}


@_safe
def hil_debug(
    target: str,
    action: str,
    firmware: str = "",
    chip: str = "",
    offset: str = "",
    verify: bool = True,
    erase: bool = False,
    halt: bool = False,
    address: str = "",
    reg: str = "",
    width: int = 32,
    count: int = 1,
    values: list[str] | None = None,
    seconds: float = 5.0,
    script: str = "",
    out: str = "",
    timeout: float = 20.0,
) -> dict:
    """On-chip flash/debug against a `debug://` target — e.g.
    ``debug://probe-rs?target=nRF52832_xxAA&transport=swd``, ``debug://auto?target=esp32``,
    or ``debug://simulator`` (hardware-free). With several probes attached, add
    ``&probe=<serial>``.

    `action` selects the verb:
      • info  — chip/core/state
      • flash — program `firmware` (.bin/.hex/.elf); uses chip/offset/verify/erase
      • erase — mass-erase flash (chip)
      • reset — reset the target (halt=True to halt at the entry point)
      • halt / resume / step — core control
      • read  — memory (`address`, `width` 8/16/32, `count`) or a register (`reg`)
      • write — memory (`address` + `values`) or a register (`reg` + `values[0]`)
      • rtt   — stream SEGGER RTT for `seconds` (optionally to file `out`)
      • gdb   — run a GDB batch `script` (optionally to file `out`)
      • probe — list probes for this target's backend

    Addresses and values are hex or decimal strings ('0x40000000', '255'), matching
    the CLI. Returns {ok, …} with the verb's result fields."""
    vals = list(values or [])
    with _instrument(target, "", timeout) as inst:
        if action == "probe":
            return {"ok": True, "probes": inst.probe_list()}
        if action == "info":
            return {"ok": True, **inst.target_info()}
        if action == "flash":
            if not firmware:
                raise HilError("hil_debug flash: 'firmware' path is required")
            res = inst.flash(
                firmware, chip=chip or "", address=offset or None, verify=verify, erase=erase
            )
            return {"ok": bool(res.get("ok", True)), **res}
        if action == "erase":
            res = inst.erase(chip=chip or "")
            return {"ok": bool(res.get("ok", True)), **res}
        if action == "reset":
            return {"ok": True, **inst.reset(halt=halt)}
        if action == "halt":
            return {"ok": True, **inst.halt()}
        if action in ("resume", "run"):
            return {"ok": True, **inst.resume()}
        if action == "step":
            return {"ok": True, **inst.step()}
        if action == "read":
            if reg:
                v = inst.read_reg(reg)
                return {"ok": True, "register": reg, "value": v, "hex": hex(int(v))}
            if address:
                words = inst.read_mem(address, width, count)
                return {
                    "ok": True,
                    "address": address,
                    "width": width,
                    "count": count,
                    "values": words,
                    "hex": [hex(w) for w in words],
                }
            raise HilError("hil_debug read: pass 'address' or 'reg'")
        if action == "write":
            if not vals:
                raise HilError("hil_debug write: 'values' is required")
            if reg:
                inst.write_reg(reg, vals[0])
                return {"ok": True, "register": reg, "value": vals[0]}
            if address:
                inst.write_mem(address, vals, width)
                return {"ok": True, "address": address, "values": vals}
            raise HilError("hil_debug write: pass 'address' or 'reg'")
        if action == "rtt":
            return {"ok": True, **inst.rtt(seconds, out=out or None)}
        if action == "gdb":
            res = inst.gdb(script, out=out or None)
            return {"ok": res.get("exit_code", 0) == 0, **res}
        raise HilError(
            f"hil_debug: unknown action '{action}'. Use one of: info, flash, erase, "
            "reset, halt, resume, step, read, write, rtt, gdb, probe."
        )


# --- tools: provisioning ----------------------------------------------------
@_safe
def hil_setup(targets: list[str] | None = None, dry_run: bool = True) -> dict:
    """Check or run backend provisioning (never surprises you with a system install).

    Two kinds of target:
    * a **python backend** — 'serial', 'visa', 'usb', 'mdns' or 'all' — installs the
      PyPI packages into THIS interpreter. Returns the exact, uv-tool-aware command
      (a plain `pip install` fails in a uv venv/tool, which has no pip). Runs it only
      when dry_run=False.
    * a **device recipe** slug (e.g. 'ppk2') — downloads/builds a third-party device
      backend; runs just the non-privileged, non-heavy steps (pip, git) when
      dry_run=False. System packages and long native builds still need a human
      `hil setup <slug> --yes`.

    With no `targets`, returns the status of every recipe plus the python-backend
    commands. Returns {ok, dry_run, ...}."""
    from . import provision, pyinstall

    plat = provision.detect()
    if not targets:
        reports = provision.status(None, platform=plat)
        return {
            "ok": True, "dry_run": True, "recipes": [r.to_dict() for r in reports],
            "python_backends": {
                name: {"packages": list(pyinstall.backend_packages(name)),
                       "command": pyinstall.install_command(pyinstall.backend_packages(name))}
                for name in ("serial", "visa", "usb", "mdns", "all")
            },
        }

    names = list(targets)
    backend_targets = [t for t in names if pyinstall.is_backend(t)]
    if backend_targets:
        pkgs: list[str] = []
        for t in backend_targets:
            for p in pyinstall.backend_packages(t):
                if p not in pkgs:
                    pkgs.append(p)
        command = pyinstall.install_command(pkgs)
        if dry_run:
            return {"ok": True, "dry_run": True, "kind": "python-backend",
                    "backends": backend_targets, "packages": pkgs, "command": command}
        import shlex
        import subprocess

        proc = subprocess.run(shlex.split(command), capture_output=True, text=True)
        return {"ok": proc.returncode == 0, "dry_run": False, "kind": "python-backend",
                "backends": backend_targets, "packages": pkgs, "command": command,
                "returncode": proc.returncode, "stderr": proc.stderr[-2000:]}

    reports = provision.provision(
        names,
        dry_run=dry_run,
        assume_yes=False,
        platform=plat,
        on_output=lambda _line: None,
    )
    return {
        "ok": all(getattr(r, "ok", True) for r in reports),
        "dry_run": dry_run,
        "reports": [r.to_dict() for r in reports],
    }


# The registered tool surface (module-level so each is unit-testable directly).
_TOOLS: tuple[Callable[..., dict], ...] = (
    hil_doctor,
    hil_devices,
    hil_device,
    hil_help,
    hil_discover,
    hil_probe,
    hil_exec,
    hil_session,
    hil_verify,
    hil_sweep,
    hil_errors,
    hil_capture,
    hil_analyze,
    hil_scrape,
    hil_debug_probe,
    hil_debug,
    hil_setup,
)


# --- resources --------------------------------------------------------------
def _register_resources(srv: Any) -> None:
    """Expose the registry as readable MCP resources (agents can browse profiles)."""

    @srv.resource(
        "hil://devices",
        mime_type="application/json",
        description="Index of every registered device profile (slug/manufacturer/model/category/driver).",
    )
    def _devices_index() -> str:
        devices = load_registry()
        return json.dumps(
            [
                {
                    "slug": d.slug,
                    "manufacturer": d.manufacturer,
                    "model": d.model,
                    "category": d.category,
                    "driver": getattr(d, "driver", "scpi"),
                }
                for d in devices.values()
            ],
            indent=2,
        )

    @srv.resource(
        "hil://devices/{slug}",
        mime_type="application/json",
        description="Full registry profile (capabilities, commands, safety policy) for one device slug.",
    )
    def _device_profile(slug: str) -> str:
        return json.dumps(hil_device(slug), indent=2, default=str)


def build_server() -> Any:
    """Construct the HIL MCP server (tools + resources).

    Raises an actionable :class:`~hil.errors.HilError` if the ``mcp`` SDK is not
    installed. Kept separate from :func:`run` so tests can introspect the tool and
    resource catalogue without opening a transport.
    """
    server_cls = _require_sdk()
    srv = server_cls(SERVER_NAME, instructions=INSTRUCTIONS)
    for fn in _TOOLS:
        srv.tool()(fn)
    _register_resources(srv)
    return srv


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the server. ``stdio`` (default) is what local coding agents spawn;
    ``http``/``streamable-http`` binds loopback only (local, never public)."""
    srv = build_server()
    t = (transport or "stdio").lower()
    if t in ("http", "streamable-http", "streamable_http", "sse"):
        settings = getattr(srv, "settings", None)
        if settings is not None:
            try:
                settings.host = host
                settings.port = port
            except Exception:  # noqa: BLE001 - settings shape varies by SDK version
                pass
        srv.run(transport="streamable-http" if t != "sse" else "sse")
    else:
        srv.run(transport="stdio")


# --- global registration for coding agents ---------------------------------
def server_command() -> list[str]:
    """The local stdio command an agent should spawn: the ``hil mcp`` console command.

    Prefer the ``hil`` executable on PATH, then the one beside the running
    interpreter (the same environment that has HIL + the mcp SDK), so the entry is
    an absolute *local command* that works from any project and survives the minimal
    PATH of GUI-launched agents. Falls back to ``<python> -m hil.mcp_server`` when no
    console script is present. This is the same local-stdio-command form as
    ``claude mcp add … -- hil mcp`` and Codex's ``[mcp_servers.hil]``.
    """
    exe = shutil.which("hil")
    if not exe:
        for name in ("hil", "hil.exe"):  # .exe for Windows console script
            adjacent = Path(sys.executable).with_name(name)
            if adjacent.exists():
                exe = str(adjacent)
                break
    if exe:
        return [exe, "mcp"]
    return [sys.executable, "-m", "hil.mcp_server"]


def server_entry(*, include_path: bool = True) -> dict:
    """The ``mcpServers``-style entry (Claude Code, Cursor, Qoder, Windsurf, VS Code, Cline).

    PATH is captured so GUI-launched agents (which start with a minimal env) can
    still find the external tools HIL shells out to — probe-rs, openocd, esptool,
    arm-none-eabi-gdb, and so on.
    """
    cmd = server_command()
    entry: dict[str, Any] = {"command": cmd[0], "args": cmd[1:]}
    if include_path:
        entry["env"] = {"PATH": os.environ.get("PATH", "")}
    return entry


def codex_toml(*, include_path: bool = True) -> str:
    """The ``[mcp_servers.hil]`` block for Codex CLI (``~/.codex/config.toml``)."""
    cmd = server_command()
    args = ", ".join(f'"{a}"' for a in cmd[1:])
    block = f'[mcp_servers.hil]\ncommand = "{cmd[0]}"\nargs = [{args}]\n'
    if include_path:
        path = os.environ.get("PATH", "").replace("\\", "\\\\").replace('"', '\\"')
        block += f'\n[mcp_servers.hil.env]\nPATH = "{path}"\n'
    return block


def _agent_specs() -> dict[str, dict[str, Any]]:
    """Where each agent keeps its USER-LEVEL (global) MCP config, and its format."""
    h = Path(os.environ.get("HIL_MCP_HOME") or Path.home())
    # QoderCN keeps its home at ~/.qoder-cn; international Qoder at ~/.qoder.
    # Prefer whichever exists so `install` targets the running flavour.
    qoder_home = h / ".qoder-cn" if (h / ".qoder-cn").exists() else h / ".qoder"
    return {
        "claude-code": {
            "path": h / ".claude.json",
            "detect": (h / ".claude.json", h / ".claude"),
            "format": "json",
            "key": "mcpServers",
        },
        "cursor": {
            "path": h / ".cursor" / "mcp.json",
            "detect": (h / ".cursor",),
            "format": "json",
            "key": "mcpServers",
        },
        "qoder": {
            "path": qoder_home / "mcp.json",
            "detect": (h / ".qoder-cn", h / ".qoder"),
            "format": "json",
            "key": "mcpServers",
        },
        "windsurf": {
            "path": h / ".codeium" / "windsurf" / "mcp_config.json",
            "detect": (h / ".codeium" / "windsurf",),
            "format": "json",
            "key": "mcpServers",
        },
        "cline": {
            "path": h / ".cline" / "mcp_settings.json",
            "detect": (h / ".cline",),
            "format": "json",
            "key": "mcpServers",
        },
        "codex": {
            "path": h / ".codex" / "config.toml",
            "detect": (h / ".codex",),
            "format": "toml",
        },
    }


def detect_agents() -> list[str]:
    """Names of agents whose global config location already exists on this host."""
    found = []
    for name, spec in _agent_specs().items():
        if any(Path(p).exists() for p in spec["detect"]):
            found.append(name)
    return found


def _write_json_agent(spec: dict, name: str, entry: dict, dry_run: bool) -> dict:
    path = Path(spec["path"])
    data: dict[str, Any] = {}
    existed = path.exists()
    if existed:
        raw = path.read_text() or "{}"
        try:
            loaded = json.loads(raw)
            data = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError as exc:
            return {
                "agent": name,
                "path": str(path),
                "ok": False,
                "error": f"existing config is not valid JSON ({exc}); refusing to overwrite",
            }
    servers = data.get(spec["key"])
    if not isinstance(servers, dict):
        servers = {}
    already = "hil" in servers
    servers["hil"] = entry
    data[spec["key"]] = servers
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return {
        "agent": name,
        "path": str(path),
        "ok": True,
        "dry_run": dry_run,
        "created": not existed,
        "changed": "updated" if already else "added",
    }


def _strip_toml_table(text: str, prefix: str = "[mcp_servers.hil") -> str:
    """Drop an existing ``[mcp_servers.hil]`` table (and its ``.env`` sub-table)."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            skipping = s.startswith(prefix)
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    return "\n".join(out)


def _write_codex_agent(spec: dict, name: str, block: str, dry_run: bool) -> dict:
    path = Path(spec["path"])
    existed = path.exists()
    text = path.read_text() if existed else ""
    already = "[mcp_servers.hil]" in text
    cleaned = _strip_toml_table(text) if already else text
    sep = "\n\n" if cleaned.strip() else ""
    new = cleaned.rstrip() + sep + block.rstrip() + "\n"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    return {
        "agent": name,
        "path": str(path),
        "ok": True,
        "dry_run": dry_run,
        "created": not existed,
        "changed": "updated" if already else "added",
    }


def install(agent: str = "", dry_run: bool = False, include_path: bool = True) -> dict:
    """Register the HIL server in agents' USER-LEVEL (global) MCP config.

    With no `agent`, targets every detected agent; otherwise just the named one.
    JSON-style configs are merged (only the ``hil`` key is touched, everything else
    preserved); Codex's TOML block is replaced idempotently. `dry_run` reports what
    would change without writing. Returns {ok, dry_run, targets:[…]}."""
    specs = _agent_specs()
    if agent:
        key = agent.strip().lower()
        if key not in specs:
            raise HilError(
                f"unknown agent '{agent}'. Known: {', '.join(sorted(specs))}."
            )
        names = [key]
    else:
        names = detect_agents()
    if not names:
        return {
            "ok": True,
            "dry_run": dry_run,
            "targets": [],
            "note": (
                "no supported agent config was detected. Pass --agent to write one "
                "anyway, or use `hil mcp config` to copy the snippet manually."
            ),
        }
    entry = server_entry(include_path=include_path)
    toml_block = codex_toml(include_path=include_path)
    targets = []
    for name in names:
        spec = specs[name]
        if spec["format"] == "toml":
            targets.append(_write_codex_agent(spec, name, toml_block, dry_run))
        else:
            targets.append(_write_json_agent(spec, name, entry, dry_run))
    return {"ok": all(t.get("ok", False) for t in targets), "dry_run": dry_run, "targets": targets}


def config_snippet(agent: str = "", include_path: bool = True) -> str:
    """Human-pasteable registration snippet for one agent, or all of them."""
    entry = server_entry(include_path=include_path)
    a = (agent or "").strip().lower()
    specs = _agent_specs()
    if a and specs.get(a, {}).get("format") == "toml":
        return f"# Codex CLI — append to {specs[a]['path']}:\n" + codex_toml(include_path=include_path)
    if a:
        path = specs.get(a, {}).get("path")
        head = f"# {a} — merge into {path}:" if path else f"# {a}:"
        return head + "\n" + json.dumps({"mcpServers": {"hil": entry}}, indent=2)
    return (
        "# JSON agents (Claude Code ~/.claude.json, Cursor ~/.cursor/mcp.json,\n"
        "#              Qoder ~/.qoder-cn/mcp.json, Windsurf, VS Code, Cline)\n"
        "#              — merge into \"mcpServers\":\n"
        + json.dumps({"mcpServers": {"hil": entry}}, indent=2)
        + "\n\n# Codex CLI — append to ~/.codex/config.toml:\n"
        + codex_toml(include_path=include_path)
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m hil.mcp_server`` entry point — run the stdio server."""
    run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
