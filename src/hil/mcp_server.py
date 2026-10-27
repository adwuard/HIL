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
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__, connect, load_registry
from .errors import HilError

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
    "HIL — Hardware-in-the-Loop bench control for the instruments and on-chip "
    "debug probes attached to THIS host. Use hil_devices/hil_device to browse the "
    "registry, hil_probe to check an instrument is alive, hil_exec to run SCPI "
    "(a trailing '?' queries), hil_capture to sample a channel, hil_debug_probe to "
    "list debuggers, and hil_debug to flash/halt/step/read/write a Cortex-M or ESP "
    "target. hil_doctor/hil_setup report and plan backend provisioning. Targets are "
    "addresses (tcp://host:5025, serial://…, visa://GPIB::24::INSTR), device slugs, "
    "or debug URLs (debug://probe-rs?target=nRF52832_xxAA&transport=swd). Every "
    "tool returns {ok, …}; on failure ok=false with an 'error' message."
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


def _safe(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn any raised error into ``{"ok": False, "error": …}``.

    Agents get one consistent structured shape from every tool (the same contract
    as :meth:`~hil.instrument.BaseInstrument.safe_execute`) instead of an MCP
    transport error. ``functools.wraps`` preserves the name/signature/docstring so
    the SDK still generates the correct tool schema.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except HilError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface any backend failure
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
    backends are importable, how many devices are registered, and which device
    backends still need provisioning (each with the exact `hil setup <slug>` hint).
    Call this first to see what hardware paths are ready."""
    from . import provision
    from .transports import backend_status

    devices = load_registry()
    prov = provision.status(platform=provision.detect())
    return {
        "ok": True,
        "hil_version": __version__,
        "python": _platform.python_version(),
        "platform": _platform.platform(),
        "backends": backend_status(),
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


# --- tools: instrument control ---------------------------------------------
@_safe
def hil_probe(target: str, timeout: float = 5.0) -> dict:
    """Connectivity check for an instrument: open it, read *IDN? and *OPC?, and
    return the parsed identity (manufacturer/model/serial/firmware) plus `ok`.
    `target` is an address (tcp://host:5025, serial://…, visa://GPIB::24::INSTR)
    or a device slug. This is the fastest way to confirm a bench link is alive."""
    with _instrument(target, "", timeout) as inst:
        identity = inst.identify()
        alive = bool(inst.opc())
    return {"ok": alive, "target": target, "identity": identity}


@_safe
def hil_exec(
    target: str,
    commands: list[str],
    device_slug: str = "",
    timeout: float = 5.0,
) -> dict:
    """Run one or more commands against an instrument in a single connection.

    A command ending in '?' is a query (its response is returned); otherwise it is
    a write. When `device_slug` is given, that profile's safety policy is enforced
    (blocked/allow/limits). Returns {ok, results:[{command, ok, query, response,
    errors, elapsed_s}]} — one structured entry per command, so a failing command
    is reported, not raised."""
    with _instrument(target, device_slug, timeout) as inst:
        results = [inst.safe_execute(c) for c in (commands or [])]
    ok = all(bool(r.get("ok", True)) for r in results)
    return {"ok": ok, "results": results}


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
        path = None
        if out:
            with DataLogger(out) as log:
                log.writemany(samples)
            path = str(out)
    return {"ok": True, "channel": channel, "captured": len(samples), "file": path, "stats": stats}


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
    """Check or plan backend provisioning (never surprises you with a system install).

    With no `targets`, returns the status of every recipe (ready or not, with the
    `hil setup <slug>` hint). With `targets`, returns the install plan; it executes
    only when dry_run=False, and even then runs just the non-privileged, non-heavy
    steps (pip installs, git clones) — system packages and long native builds still
    require a human `hil setup <slug> --yes`."""
    from . import provision

    plat = provision.detect()
    if not targets:
        reports = provision.status(None, platform=plat)
        return {"ok": True, "dry_run": True, "recipes": [r.to_dict() for r in reports]}
    reports = provision.provision(
        list(targets),
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
    hil_probe,
    hil_exec,
    hil_errors,
    hil_capture,
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
    """The absolute command an agent should spawn.

    Uses *this* interpreter + ``-m hil.mcp_server`` so it works regardless of the
    agent's PATH and always matches the environment that has HIL (and the mcp SDK)
    installed — the key to being 'accessible globally' from any project.
    """
    return [sys.executable, "-m", "hil.mcp_server"]


def server_entry(*, include_path: bool = True) -> dict:
    """The ``mcpServers``-style entry (Claude Code, Cursor, Windsurf, VS Code, Cline).

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
        "#              Windsurf, VS Code, Cline) — merge into \"mcpServers\":\n"
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
