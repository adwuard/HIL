"""`hil` command-line interface — agent-first: every command speaks --json.

    hil probe tcp://192.168.1.5:5025          # is it alive? clear diagnostics
    hil idn keithley-2400                     # *IDN? via registry
    hil exec <addr> '*IDN?' 'MEAS:VOLT?'     # run commands; '?' = query
    hil errors <addr>                         # drain SYST:ERR?
    hil devices list | show <slug>            # registry
    hil log <addr> --channel :MEAS:CURR? --seconds 30 --out run.ndjson
    hil analyze run.ndjson                    # summarize a capture
    hil doctor                                # env + backend availability
    hil setup [target] [--dry-run|--yes]      # fetch/build device backends on demand
    hil debug probe debug://probe-rs          # list attached on-chip debug probes
    hil debug flash debug://auto?target=esp32 fw.bin   # flash firmware (routes by chip)
    hil atklogic info                         # identify the attached ATK-Logic analyzer
    hil atklogic capture --channels 0,1 --rate 1e6 --depth 200000 --out cap.atkdl
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Sequence

from . import __version__, connect, load_registry
from .errors import HilError
from .registry import Device
from .telemetry import DataLogger, iter_channels, read_samples, summarize
from .transports import backend_status


def _build_parser() -> argparse.ArgumentParser:
    # Global flags live on a parent so they work both before and after the
    # subcommand: `hil --json exec ...` and `hil exec ... --json`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--timeout", type=float, default=5.0, help="seconds (default 5)")

    # On subparsers, defaults must not clobber values set at the top level.
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub_common.add_argument("--timeout", type=float, default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="hil",
        description="Agent harness for SCPI instruments (Python + CLI + agents).",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def addr_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "address",
            help="address (tcp://host:5025, serial://..., visa://GPIB::24::INSTR) "
            "or device slug from 'hil devices list'",
        )

    p = sub.add_parser("probe", help="connect, *IDN?, *OPC? — connectivity check", parents=[sub_common])
    addr_arg(p)

    p = sub.add_parser("idn", help="query *IDN? and parse identity", parents=[sub_common])
    addr_arg(p)

    p = sub.add_parser("exec", help="send SCPI command(s); trailing '?' = query", parents=[sub_common])
    addr_arg(p)
    p.add_argument("commands", nargs="+", help="SCPI command(s) to run in order")
    p.add_argument("--device", help="slug for safety policy when using a raw address")

    p = sub.add_parser("errors", help="drain the SYSTem:ERRor? queue", parents=[sub_common])
    addr_arg(p)
    p.add_argument("--device", help="slug for safety policy when using a raw address")

    p = sub.add_parser("devices", help="device registry", parents=[sub_common])
    dsub = p.add_subparsers(dest="devices_cmd", required=True)
    dsub.add_parser("list", help="list known devices", parents=[sub_common])
    dp = dsub.add_parser("show", help="show one device definition", parents=[sub_common])
    dp.add_argument("slug")

    p = sub.add_parser(
        "help",
        help="look up the correct SCPI syntax for a model (reference corpus)",
        parents=[sub_common],
    )
    p.add_argument(
        "model",
        help="model, slug, or *IDN? string (e.g. 'DP932E', 'Rigol DP932E')",
    )
    p.add_argument("--command", default="", help="filter by mnemonic fragment (e.g. OUTP:STAT)")
    p.add_argument("--subsystem", default="", help="filter by subsystem (e.g. MEASure)")
    p.add_argument("--limit", type=int, default=40, help="max commands to return (default 40)")

    p = sub.add_parser(
        "discover",
        help="enumerate the instruments attached to THIS host right now",
        parents=[sub_common],
    )
    p.add_argument(
        "--interfaces", default="",
        help="comma list of usb,serial,lan,debug (default: all)",
    )
    p.add_argument(
        "--no-identify", action="store_true",
        help="skip the per-device *IDN? round-trip (faster, no model match)",
    )

    _add_scrape_parser(sub, sub_common)
    _add_bench_parser(sub, sub_common)
    _add_telemetry_parsers(sub, sub_common)
    _add_debug_parser(sub, sub_common)
    _add_atklogic_parser(sub, sub_common)

    sub.add_parser("doctor", help="environment and backend availability", parents=[sub_common])

    p = sub.add_parser(
        "setup",
        help="download/build optional third-party device backends on demand",
        parents=[sub_common],
    )
    p.add_argument(
        "targets", nargs="*",
        help="python backend (serial/visa/usb/mdns/all), or a device slug / driver / "
        "recipe slug (default: status of every recipe)",
    )
    p.add_argument("--list", action="store_true", help="list provisioning recipes and exit")
    p.add_argument("--check", action="store_true", help="only report status; change nothing")
    p.add_argument("--dry-run", action="store_true",
                   help="print the exact commands without running them")
    p.add_argument("--yes", action="store_true",
                   help="allow privileged steps (sudo/brew/apt native installs)")

    _add_mcp_parser(sub, sub_common)
    return parser


def _add_scrape_parser(
    sub: argparse._SubParsersAction, sub_common: argparse.ArgumentParser
) -> None:
    """`hil scrape {catalog,device,all}` — reference site -> registry + manifest."""
    p = sub.add_parser(
        "scrape", help="scrape instrument SCPI references", parents=[sub_common]
    )
    ssub = p.add_subparsers(dest="scrape_cmd", required=True)

    def common_scrape(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--out", default="scraped", help="output directory (default: ./scraped)"
        )
        sp.add_argument(
            "--cache-dir",
            default=".cache/reference",
            help="HTTP cache dir; makes re-runs/offline free (default: ./.cache/reference)",
        )
        sp.add_argument(
            "--no-cache", action="store_true", help="disable the HTTP cache"
        )
        sp.add_argument(
            "--offline",
            action="store_true",
            help="serve strictly from cache; never hit the network",
        )
        sp.add_argument(
            "--delay",
            type=float,
            default=0.4,
            help="min seconds between network requests (default: 0.4)",
        )

    cp = ssub.add_parser(
        "catalog", help="discover every device (1 request) -> catalog.json",
        parents=[sub_common],
    )
    common_scrape(cp)

    dp = ssub.add_parser(
        "device", help="scrape one instrument -> YAML + JSON", parents=[sub_common]
    )
    dp.add_argument("slug")
    common_scrape(dp)
    dp.add_argument(
        "--format",
        choices=("yaml", "json", "both"),
        default="both",
        help="artefacts to write (default: both)",
    )

    ap = ssub.add_parser(
        "all", help="full crawl -> per-device files + manifest.json",
        parents=[sub_common],
    )
    common_scrape(ap)
    ap.add_argument("--format", choices=("yaml", "json", "both"), default="json",
                    help="bulk default is json (lossless, compact); use both for registry YAML")
    ap.add_argument("--limit", type=int, default=None, help="cap number of devices")
    ap.add_argument(
        "--category", action="append", default=[], help="only this category (repeatable)"
    )
    ap.add_argument(
        "--vendor", action="append", default=[], help="only this vendor (repeatable)"
    )
    ap.add_argument(
        "--force", action="store_true", help="rewrite artefacts even if present"
    )
    ap.add_argument(
        "--dataset-version",
        default="",
        help="stamp manifest.json with a dataset version (e.g. v1)",
    )
    ap.add_argument(
        "--retries", type=int, default=3, help="per-request retries (default: 3)"
    )


def _add_bench_parser(
    sub: argparse._SubParsersAction, sub_common: argparse.ArgumentParser
) -> None:
    """`hil bench {run,validate,list,init}` — the agentic HIL test bench."""
    p = sub.add_parser(
        "bench", help="run declarative HIL test procedures", parents=[sub_common]
    )
    bsub = p.add_subparsers(dest="bench_cmd", required=True)

    def spec_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("spec", help="procedure file (YAML or JSON)")

    rp = bsub.add_parser(
        "run", help="run a procedure (dry-runs on the simulator by default)",
        parents=[sub_common],
    )
    spec_arg(rp)
    rp.add_argument("--device", help="registry slug supplying the safety policy")
    rp.add_argument(
        "--address",
        help="live target (tcp://host:5025, visa://...); omit to dry-run on the simulator",
    )
    rp.add_argument(
        "--sim", action="store_true",
        help="force the simulator even if an address is set",
    )
    rp.add_argument(
        "--continue-on-error", action="store_true",
        help="run every step even after one fails",
    )
    rp.add_argument(
        "--report-out", help="also write the JSON run report to this path"
    )
    rp.add_argument("--md", action="store_true", help="print the report as Markdown")
    rp.add_argument(
        "--registry-dir", action="append", default=[],
        help="extra device-registry dir (repeatable), e.g. scraped/devices",
    )

    vp = bsub.add_parser(
        "validate", help="check a procedure without running it", parents=[sub_common]
    )
    spec_arg(vp)

    lp = bsub.add_parser("list", help="list a procedure's steps", parents=[sub_common])
    spec_arg(lp)

    ip = bsub.add_parser(
        "init", help="write a sample procedure to start from", parents=[sub_common]
    )
    ip.add_argument(
        "path", nargs="?", default="bench/example.yaml",
        help="output file (default: bench/example.yaml)",
    )
    ip.add_argument("--force", action="store_true", help="overwrite if it exists")


def _add_telemetry_parsers(
    sub: argparse._SubParsersAction, sub_common: argparse.ArgumentParser
) -> None:
    """`hil log` (capture) and `hil analyze` (summarize) — long-run telemetry."""
    lp = sub.add_parser(
        "log", help="capture timestamped telemetry to a file", parents=[sub_common]
    )
    lp.add_argument(
        "address",
        help="address (tcp://host:5025, ppk2://PORT, ...) or device slug",
    )
    lp.add_argument(
        "--channel", required=True,
        help="query/channel to sample, e.g. ':MEAS:CURR?' (SCPI) or 'current'/'power' (PPK2)",
    )
    lp.add_argument("--seconds", type=float, default=10.0, help="capture duration (default 10)")
    lp.add_argument(
        "--interval", type=float, default=None,
        help="seconds between polls for query devices (default 0.1)",
    )
    lp.add_argument("--units", default="", help="unit label for samples (A, V, W, ...)")
    lp.add_argument("--out", default=None, help="capture file (.ndjson default, or .csv)")
    lp.add_argument("--device", help="slug for safety policy when using a raw address")
    lp.add_argument(
        "--setup", action="append", default=[], metavar="CMD",
        help="command(s) sent before capture, in order (repeatable) — e.g. for a "
             "PPK2: --setup ':CONF:MODE AMP' --setup ':SOUR:VOLT 3300'",
    )
    lp.add_argument("--sim", action="store_true", help="hardware-free dry-run on the simulator")
    lp.add_argument("--sim-value", default="1.0", help="simulated reply for --sim (default 1.0)")

    ap = sub.add_parser(
        "analyze", help="summarize a captured telemetry file", parents=[sub_common]
    )
    ap.add_argument("file", help="capture file (.ndjson/.jsonl/.csv)")
    ap.add_argument("--md", action="store_true", help="print a Markdown table")


def _add_debug_parser(
    sub: argparse._SubParsersAction, sub_common: argparse.ArgumentParser
) -> None:
    """`hil debug {probe,info,flash,erase,reset,halt,resume,step,read,write,gdb,rtt}`.

    The imperative face of the flash/debug driver (the bench ``flash``/``debug``
    steps are the declarative face). ``address`` is a ``debug://`` target or a
    device slug; ``--sim`` runs on the in-memory simulator with no probe attached.
    """
    p = sub.add_parser(
        "debug",
        help="flash + on-chip debug (probe-rs/openocd/esptool/stlink/pyocd)",
        parents=[sub_common],
    )
    dsub = p.add_subparsers(dest="debug_cmd", required=True)

    def target_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "address", nargs="?", default="",
            help="debug://BACKEND[?target=chip&probe=id] or a device slug",
        )
        sp.add_argument("--device", help="slug for safety policy when using a raw address")
        sp.add_argument("--sim", action="store_true",
                        help="run on the in-memory simulator target (no probe needed)")

    sp = dsub.add_parser("probe", help="list attached debug probes", parents=[sub_common])
    target_arg(sp)
    sp = dsub.add_parser("info", help="show target chip/core/state", parents=[sub_common])
    target_arg(sp)

    sp = dsub.add_parser("flash", help="program firmware onto the target", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("firmware", help="firmware image (.bin/.hex/.elf)")
    sp.add_argument("--chip", help="chip/target name (e.g. nrf52840, stm32f4x, esp32)")
    sp.add_argument("--offset", help="flash address (e.g. 0x08000000); tool default if omitted")
    sp.add_argument("--no-verify", action="store_true", help="skip read-back verify")
    sp.add_argument("--erase", action="store_true", help="mass-erase before programming")

    sp = dsub.add_parser("erase", help="erase target flash", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("--chip", help="chip/target name")

    sp = dsub.add_parser("reset", help="reset (and optionally halt) the target", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("--halt", action="store_true", help="halt after reset")

    for name, hlp in (("halt", "halt the core"), ("resume", "resume/run the core"),
                      ("step", "single-step one instruction")):
        sp = dsub.add_parser(name, help=hlp, parents=[sub_common])
        target_arg(sp)

    sp = dsub.add_parser("read", help="read memory or a register", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("--mem", help="memory address (e.g. 0x40000000)")
    sp.add_argument("--reg", help="register name (e.g. pc, sp, r0)")
    sp.add_argument("--width", type=int, default=32, choices=(8, 16, 32),
                    help="access width in bits (default 32)")
    sp.add_argument("--count", type=int, default=1, help="words to read (default 1)")

    sp = dsub.add_parser("write", help="write memory or a register", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("--mem", help="memory address (e.g. 0x20000000)")
    sp.add_argument("--reg", help="register name (e.g. r0)")
    sp.add_argument("--value", nargs="+", required=True, help="value(s) to write (hex/dec)")
    sp.add_argument("--width", type=int, default=32, choices=(8, 16, 32),
                    help="access width in bits (default 32)")

    sp = dsub.add_parser("gdb", help="run a GDB batch script against the target", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("script", help="GDB command script (passed as -x)")
    sp.add_argument("--out", help="write GDB output to this file")

    sp = dsub.add_parser("rtt", help="stream SEGGER RTT output for a few seconds", parents=[sub_common])
    target_arg(sp)
    sp.add_argument("--seconds", type=float, default=5.0, help="capture duration (default 5)")
    sp.add_argument("--out", help="write the RTT log to this file (default: spool)")


def _add_atklogic_parser(
    sub: argparse._SubParsersAction, sub_common: argparse.ArgumentParser
) -> None:
    """`hil atklogic {info,list,capture,decode,convert}`.

    Direct, in-process control of an ALIENTEK ATK-Logic-Analyzer (USB ``1A86:FFCC``)
    through the vendored MIT core — **no capture daemon**. ``info``/``capture`` need
    the analyzer attached; ``list``/``decode``/``convert`` run offline on saved
    artifacts. Reuses :class:`hil.drivers._atklogic.AtkCore`, the same core the
    ``logic://usb`` backend drives, so the two can never drift.
    """
    p = sub.add_parser(
        "atklogic",
        help="ATK-Logic-Analyzer in-process (info/list/capture/decode/convert)",
        parents=[sub_common],
    )
    asub = p.add_subparsers(dest="atklogic_cmd", required=True)

    def dev_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--serial", help="device serial (default: first ATK-Logic found)")
        sp.add_argument("--decoders", help="sigrok decoders dir (default: $ATK_DECODERS_DIR)")
        sp.add_argument("--libusb", help="libusb-1.0 path hint (default: auto-discover)")

    sp = asub.add_parser("info", help="identify the attached analyzer", parents=[sub_common])
    dev_args(sp)

    sp = asub.add_parser("list", help="list available protocol decoders", parents=[sub_common])
    dev_args(sp)
    sp.add_argument("filter", nargs="?", default="", help="substring filter (e.g. 'i2c')")

    sp = asub.add_parser("capture", help="acquire samples and save an artifact", parents=[sub_common])
    dev_args(sp)
    sp.add_argument("--channels", default="0",
                    help="channel list, e.g. '0', '0,1', 'ch0,ch7' (default 0)")
    sp.add_argument("--rate", type=float, default=1e6,
                    help="sample rate in Hz, snapped to a device gear (default 1e6)")
    sp.add_argument("--depth", type=int, default=100000, help="samples to capture (default 100000)")
    sp.add_argument("--threshold", type=float, default=1.5, help="logic threshold volts (default 1.5)")
    sp.add_argument("--trigger", default="rising",
                    help="rising|falling|any|high|low (default rising)")
    sp.add_argument("--mode", default="buffer", choices=("buffer", "trigger"),
                    help="capture mode (default buffer)")
    sp.add_argument("--rle", action="store_true", help="enable run-length encoding on the wire")
    sp.add_argument("--fmt", default="atkdl", choices=("atkdl", "csv", "raw"),
                    help="output format (default atkdl)")
    sp.add_argument("--out", required=True, help="output artifact path")

    sp = asub.add_parser("decode", help="decode a saved artifact with sigrok PDs", parents=[sub_common])
    dev_args(sp)
    sp.add_argument("artifact", help="capture artifact (.atkdl or .csv)")
    sp.add_argument("--decoder", action="append", default=[],
                    help="decoder name (repeatable to build a stack)")
    sp.add_argument("--map", dest="map", default="",
                    help="first-decoder channel map, e.g. 'rx=0,tx=1'")
    sp.add_argument("--option", action="append", default=[],
                    help="decoder option k=v (repeatable, first decoder)")
    sp.add_argument("--max-samples", type=int, default=4000000, help="decode sample cap (default 4e6)")
    sp.add_argument("--max-items", type=int, default=200, help="annotation/transaction cap (default 200)")
    sp.add_argument("--fmt", default="csv", choices=("csv", "json"),
                    help="output format when --out is set (default csv)")
    sp.add_argument("--out", help="write decoded rows here (default: print summary only)")

    sp = asub.add_parser("convert", help="re-serialize a saved capture to csv/raw", parents=[sub_common])
    dev_args(sp)
    sp.add_argument("artifact", help="source capture artifact (.atkdl or .csv)")
    sp.add_argument("--channels", default="", help="channel subset, e.g. '0,1' (default: all in file)")
    sp.add_argument("--fmt", default="csv", choices=("csv", "raw"), help="target format (default csv)")
    sp.add_argument("--out", required=True, help="output path")


def _add_mcp_parser(
    sub: argparse._SubParsersAction, sub_common: argparse.ArgumentParser
) -> None:
    """`hil mcp [install|config]` — run the HIL MCP server and register it globally.

    With no subcommand ``hil mcp`` serves HIL's tools over stdio (what a coding
    agent spawns) or loopback HTTP. ``install`` merges the server into detected
    agents' USER-LEVEL config so it's available in every project; ``config`` prints
    the snippet to paste by hand.
    """
    p = sub.add_parser(
        "mcp",
        help="run/register the HIL MCP server for coding agents (Claude Code, Codex, Cursor, …)",
        parents=[sub_common],
    )
    p.add_argument("--transport", default="stdio",
                   choices=("stdio", "http", "streamable-http", "sse"),
                   help="stdio (default; what agents spawn) or loopback http/sse")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind host for --transport http (default 127.0.0.1, loopback only)")
    p.add_argument("--port", type=int, default=8765,
                   help="bind port for --transport http (default 8765)")
    msub = p.add_subparsers(dest="mcp_cmd")

    mi = msub.add_parser("install", help="register HIL in agents' global MCP config",
                         parents=[sub_common])
    mi.add_argument("--agent", default="",
                    help="target one agent (claude-code, codex, cursor, qoder, windsurf, cline); "
                         "default: every detected agent")
    mi.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    mi.add_argument("--no-path", action="store_true",
                    help="don't bake the current PATH into the server env")

    mc = msub.add_parser("config", help="print the registration snippet to paste manually",
                         parents=[sub_common])
    mc.add_argument("--agent", default="", help="snippet for one agent (default: all)")
    mc.add_argument("--no-path", action="store_true", help="omit PATH from the snippet env")


# -- output helpers -----------------------------------------------------------


def _emit(args: argparse.Namespace, payload: object, human: str) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(human)


def _device_rows(device: Device) -> dict[str, object]:
    data = asdict(device)
    data["commands"] = [asdict(c) for c in device.commands]
    data["safety"] = asdict(device.safety)
    return data


# -- subcommands ---------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    inst = connect(args.address, timeout=args.timeout)
    try:
        identity = inst.identify()
        ok = inst.opc()
    finally:
        inst.close()
    _emit(
        args,
        {"ok": ok, "address": args.address, "identity": identity},
        "\n".join(
            [
                f"OK  {args.address}",
                f"  manufacturer: {identity['manufacturer']}",
                f"  model:        {identity['model']}",
                f"  serial:       {identity['serial']}",
                f"  firmware:     {identity['firmware']}",
            ]
        ),
    )
    return 0


def cmd_idn(args: argparse.Namespace) -> int:
    inst = connect(args.address, timeout=args.timeout)
    try:
        raw = inst.idn()
        identity = inst.identify()
    finally:
        inst.close()
    _emit(args, {"idn": raw, **identity}, raw)
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    inst = connect(
        args.address, device_slug=args.device, timeout=args.timeout
    )
    try:
        results = [inst.safe_execute(c) for c in args.commands]
    finally:
        inst.close()
    ok = all(r["ok"] for r in results)  # type: ignore[index]
    if args.json:
        _emit(args, {"ok": ok, "results": results}, "")
    else:
        lines = []
        for r in results:  # type: ignore[index]
            if not r["ok"]:
                err = r.get("error") or "; ".join(r.get("errors") or []) or "failed"
                lines.append(f"{r['command']} -> ERROR {err}")
            elif r.get("response") is not None:
                lines.append(f"{r['command']} -> {r['response']}")
            else:
                lines.append(f"{r['command']} -> ok")
        _emit(args, {}, "\n".join(lines))
    return 0 if ok else 1


def cmd_errors(args: argparse.Namespace) -> int:
    inst = connect(
        args.address, device_slug=args.device, timeout=args.timeout
    )
    try:
        errors = inst.drain_errors()
    finally:
        inst.close()
    if args.json:
        _emit(args, {"errors": errors}, "")
    else:
        _emit(
            args,
            {},
            "\n".join(f"{e['code']}: {e['message']}" for e in errors)
            or "queue empty (no errors)",
        )
    return 1 if errors else 0


def cmd_devices(args: argparse.Namespace) -> int:
    devices = load_registry()
    if args.devices_cmd == "list":
        if args.json:
            _emit(
                args,
                {
                    "devices": [
                        {
                            "slug": d.slug,
                            "manufacturer": d.manufacturer,
                            "model": d.model,
                            "category": d.category,
                            "interfaces": list(d.interfaces),
                        }
                        for d in devices.values()
                    ]
                },
                "",
            )
        else:
            rows = "\n".join(
                f"{d.slug:<22} {d.manufacturer:<24} {d.model:<16} {d.category}"
                for d in devices.values()
            )
            _emit(args, {}, f"{'slug':<22} {'manufacturer':<24} {'model':<16} category\n{rows}")
        return 0

    # show
    device = devices.get(args.slug)
    if device is None:
        raise HilError(
            f"Unknown device slug '{args.slug}'. Known: {', '.join(sorted(devices)) or '(none)'}."
        )
    _emit(args, _device_rows(device), "")
    return 0


def cmd_help(args: argparse.Namespace) -> int:
    """`hil help <model>` — the correct SCPI for an exact model from the corpus."""
    from . import reference

    result = reference.help_lookup(
        args.model, subsystem=args.subsystem, command=args.command, limit=args.limit
    )
    if args.json:
        _emit(args, result, "")
        return 0 if result.get("ok") else 1

    matched = result.get("matched") or {}
    if not result.get("ok"):
        print(result.get("hint", "no match"), file=sys.stderr)
        for n in result.get("near", []):
            print(f"  near: {n['slug']:<24} {n['name']} ({n['score']})", file=sys.stderr)
        return 1
    dev = result.get("device", {})
    head = matched.get("name") or dev.get("name") or matched.get("slug")
    print(f"{head}  ({matched.get('slug')})")
    if dev.get("model"):
        print(f"  vendor/model: {dev.get('manufacturer','')} / {dev.get('model')}")
    if dev.get("manual"):
        print(f"  manual:       {dev['manual']}")
    cmds = result.get("commands", [])
    print(f"  commands:     {len(cmds)}"
          + (f" (filtered by {args.command or args.subsystem})" if (args.command or args.subsystem) else ""))
    for c in cmds:
        q = "?" if c.get("query") else " "
        params = ", ".join(p.get("name", "") for p in c.get("parameters", []))
        line = f"    {c.get('mnemonic','')}{q}"
        if params:
            line += f"  ({params})"
        desc = (c.get("description") or "").strip()
        if desc:
            line += f"  — {desc[:70]}"
        print(line)
    if result.get("hint"):
        print(f"\n  note: {result['hint']}", file=sys.stderr)
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """`hil discover` — enumerate the instruments actually attached right now."""
    from . import discovery

    interfaces = tuple(
        i.strip() for i in (args.interfaces or "").split(",") if i.strip()
    ) or discovery.DEFAULT_INTERFACES
    timeout = 3.0 if args.timeout <= 5.0 else args.timeout

    def progress(msg: str) -> None:
        if not args.json:
            print(msg, file=sys.stderr)

    result = discovery.discover(
        interfaces, identify=not args.no_identify, timeout=timeout, on_progress=progress
    )
    if args.json:
        _emit(args, result, "")
        return 0

    devices = result.get("devices", [])
    backends = result.get("backends", {})
    ready = ", ".join(k for k, v in backends.items() if v) or "(none)"
    print(f"discovered {len(devices)} device(s)  [backends ready: {ready}]")
    for d in devices:
        name = d.get("matched_name") or (d.get("identity", {}) or {}).get("model") or d.get("transport", "")
        print(f"  {d.get('address','')}" + (f"   {name}" if name else ""))
        if d.get("idn"):
            print(f"      *IDN? {d['idn']}")
        if d.get("matched_slug"):
            print(f"      ref:  {d['matched_slug']}  (hil help {d['matched_slug']})")
    for w in result.get("warnings", []):
        print(f"  [warn] {w}", file=sys.stderr)
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .scraper import Fetcher, run_all, run_catalog, run_device

    # The global --timeout defaults to 5s, too small for 1-3 MB pages; use a
    # generous scrape default unless the caller asked for something larger.
    timeout = 60.0 if args.timeout <= 5.0 else args.timeout
    cache_dir = None if args.no_cache else Path(args.cache_dir)
    fetcher = Fetcher(
        cache_dir=cache_dir,
        timeout=timeout,
        offline=args.offline,
        min_delay=getattr(args, "delay", 0.4),
        retries=getattr(args, "retries", 3),
    )
    out = Path(args.out)

    if args.scrape_cmd == "catalog":
        report = run_catalog(out, fetcher)
        payload = {
            "ok": True,
            "count": len(report.catalog),
            "catalog_path": str(report.catalog_path),
            "requests": fetcher.requests_made,
            "cache_hits": fetcher.cache_hits,
        }
        _emit(
            args,
            payload,
            f"catalog: {len(report.catalog)} devices -> {report.catalog_path}",
        )
        return 0

    if args.scrape_cmd == "device":
        report = run_device(out, fetcher, args.slug, fmt=args.format)
        dev = report.devices[0]
        written = sorted((out / "devices").glob(f"{dev.slug}.*"))
        payload = {
            "ok": True,
            "slug": dev.slug,
            "name": dev.name,
            "manufacturer": dev.manufacturer,
            "model": dev.model,
            "series": dev.series,
            "category": dev.category,
            "url": dev.url,
            "stats": dev.stats(),
            "advertised": dev.advertised,
            "files": [str(p) for p in written],
        }
        _emit(
            args,
            payload,
            "\n".join(
                [
                    f"{dev.name or dev.slug}  ({dev.slug})",
                    f"  vendor/series: {dev.manufacturer} / {dev.series or '-'}",
                    f"  commands:      {dev.command_count} "
                    f"({dev.query_count} queries, {dev.setter_count} setters)",
                    f"  subsystems:    {dev.subsystem_count}",
                    f"  manual:        {dev.manual or '-'} ({dev.date_modified or '-'})",
                    f"  wrote:         {', '.join(str(p) for p in written)}",
                ]
            ),
        )
        return 0

    # all
    def progress(done: int, total: int, slug: str, ok: bool) -> None:
        if not args.json:
            print(
                f"[{done}/{total}] {'ok  ' if ok else 'FAIL'} {slug}",
                file=sys.stderr,
            )

    report = run_all(
        out,
        fetcher,
        limit=args.limit,
        categories=tuple(args.category),
        vendors=tuple(args.vendor),
        fmt=args.format,
        force=args.force,
        dataset_version=getattr(args, "dataset_version", ""),
        progress=progress,
    )
    totals = {
        "devices_scraped": len(report.devices),
        "commands": sum(d.command_count for d in report.devices),
        "failed": len(report.failed),
    }
    payload = {
        "ok": not report.failed,
        **totals,
        "catalog_count": len(report.catalog),
        "manifest_path": str(report.manifest_path),
        "catalog_path": str(report.catalog_path),
        "requests": fetcher.requests_made,
        "cache_hits": fetcher.cache_hits,
        "failed_devices": [{"slug": s, "error": e} for s, e in report.failed],
    }
    if args.json:
        _emit(args, payload, "")
    else:
        lines = [
            f"scraped {totals['devices_scraped']} devices "
            f"({totals['commands']} commands) -> {out}",
            f"  manifest: {report.manifest_path}",
            f"  catalog:  {report.catalog_path} ({len(report.catalog)} known)",
        ]
        if report.failed:
            lines.append(f"  FAILED:   {len(report.failed)} (see --json)")
        _emit(args, {}, "\n".join(lines))
    return 0 if not report.failed else 1


def cmd_bench(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .bench import (
        SAMPLE_SPEC,
        build_instrument_factory,
        load_procedure,
        resolve_run_target,
        run_procedure,
    )
    from .errors import ProcedureError

    if args.bench_cmd == "init":
        path = Path(args.path)
        if path.exists() and not args.force:
            raise ProcedureError(f"{path} already exists (use --force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SAMPLE_SPEC, encoding="utf-8")
        _emit(args, {"ok": True, "path": str(path)}, f"wrote sample procedure -> {path}")
        return 0

    try:
        proc = load_procedure(args.spec)
    except ProcedureError as exc:
        if args.bench_cmd == "validate":
            if args.json:
                _emit(args, {"ok": False, "spec": args.spec, "error": str(exc)}, "")
            else:
                print(f"invalid: {exc}", file=sys.stderr)
            return 1
        raise

    if args.bench_cmd == "validate":
        _emit(
            args,
            {"ok": True, "spec": args.spec, "name": proc.name,
             "device": proc.device, "steps": len(proc.steps)},
            f"valid: {proc.name} — {len(proc.steps)} steps"
            + (f", device={proc.device}" if proc.device else ""),
        )
        return 0

    if args.bench_cmd == "list":
        if args.json:
            _emit(
                args,
                {"name": proc.name,
                 "steps": [{"index": s.index, "type": s.type, "summary": s.summary()}
                           for s in proc.steps]},
                "",
            )
        else:
            lines = [f"{proc.name} — {len(proc.steps)} steps"]
            lines += [f"  [{s.index:>2}] {s.type:<8} {s.summary()}" for s in proc.steps]
            _emit(args, {}, "\n".join(lines))
        return 0

    # run
    mode, target, slug = resolve_run_target(
        proc, sim=args.sim, address=args.address, device=args.device
    )
    registry_dirs = tuple(Path(d) for d in getattr(args, "registry_dir", []))
    factory = build_instrument_factory(
        proc, mode=mode, target=target, device_slug=slug,
        timeout=args.timeout, registry_dirs=registry_dirs,
    )

    def on_step(sr: object) -> None:
        mark = "skip" if sr.skipped else ("ok  " if sr.ok else "FAIL")  # type: ignore[attr-defined]
        print(f"  [{sr.index:>2}] {mark} {sr.summary}", file=sys.stderr)  # type: ignore[attr-defined]

    report = run_procedure(
        proc, factory, mode=mode, target=target,
        continue_on_error=args.continue_on_error or None,
        on_step=on_step if (mode == "live" and not args.json) else None,
    )
    if args.report_out:
        out = Path(args.report_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    elif args.md:
        print(report.markdown())
    else:
        print(report.human())
    return 0 if report.ok else 1


def _debug_dispatch(action: str, args: argparse.Namespace, inst) -> tuple[dict, str]:
    """Run one ``hil debug`` action; return a ``(payload, human)`` pair."""
    if action == "probe":
        probes = inst.probe_list()
        lines = [f"{len(probes)} probe(s)" + (":" if probes else "")]
        for i, p in enumerate(probes):
            if not isinstance(p, dict):
                lines.append(f"  {p}")
                continue
            label = f"  [{p.get('index', i)}] {p.get('name') or p.get('id') or 'probe'}"
            if p.get("type"):
                label += f"  ({p['type']})"
            if p.get("serial"):
                label += f"  sn {p['serial']}"
            lines.append(label)
        return {"ok": True, "probes": probes}, "\n".join(lines)

    if action == "info":
        info = inst.target_info()
        return {"ok": True, **info}, "\n".join(f"{k}: {v}" for k, v in info.items()) or "no info"

    if action == "flash":
        res = inst.flash(args.firmware, chip=args.chip or "", address=args.offset,
                         verify=not args.no_verify, erase=args.erase)
        human = (f"flashed {args.firmware} via {res.get('tool')} "
                 f"sha256={str(res.get('sha256', ''))[:12]} verified={res.get('verified')} "
                 f"size={res.get('size_bytes')} in {res.get('duration_s')}s")
        return {"ok": bool(res.get("ok", True)), **res}, human

    if action == "erase":
        res = inst.erase(chip=args.chip or "")
        return {"ok": bool(res.get("ok", True)), **res}, \
            f"erased via {res.get('tool')} ok={res.get('ok')}"

    if action == "reset":
        res = inst.reset(halt=args.halt)
        return {"ok": True, **res}, f"reset -> state={res.get('state')}"

    if action == "halt":
        res = inst.halt()
        return {"ok": True, **res}, f"state={res.get('state')} pc={res.get('pc', '')}"

    if action in ("resume", "run"):
        res = inst.resume()
        return {"ok": True, **res}, f"state={res.get('state')}"

    if action == "step":
        res = inst.step()
        return {"ok": True, **res}, f"stepped -> pc={res.get('pc', '')}"

    if action == "read":
        if args.reg:
            val = inst.read_reg(args.reg)
            return {"ok": True, "register": args.reg, "value": val, "hex": hex(int(val))}, \
                f"{args.reg} = {hex(int(val))}"
        if args.mem:
            words = inst.read_mem(args.mem, args.width, args.count)
            return {"ok": True, "address": args.mem, "width": args.width,
                    "count": args.count, "values": words, "hex": [hex(w) for w in words]}, \
                f"{args.mem}: " + " ".join(hex(w) for w in words)
        raise HilError("hil debug read: pass --mem ADDR or --reg NAME")

    if action == "write":
        if args.reg:
            inst.write_reg(args.reg, args.value[0])
            return {"ok": True, "register": args.reg, "value": args.value[0]}, \
                f"{args.reg} <- {args.value[0]}"
        if args.mem:
            inst.write_mem(args.mem, args.value, args.width)
            return {"ok": True, "address": args.mem, "value": args.value}, \
                f"{args.mem} <- {', '.join(map(str, args.value))}"
        raise HilError("hil debug write: pass --mem ADDR or --reg NAME")

    if action == "gdb":
        res = inst.gdb(args.script, out=args.out)
        return {"ok": res.get("exit_code", 0) == 0, **res}, \
            f"gdb exit={res.get('exit_code')}" + (f" -> {res.get('path')}" if res.get("path") else "")

    if action == "rtt":
        res = inst.rtt(args.seconds, out=args.out)
        return {"ok": True, **res}, \
            f"rtt {res.get('line_count', 0)} lines -> {res.get('path', '')}"

    raise HilError(f"hil debug: unknown action '{action}'")


def cmd_debug(args: argparse.Namespace) -> int:
    target = "debug://simulator" if getattr(args, "sim", False) else (args.address or "")
    if not target:
        raise HilError(
            "hil debug: pass an address (e.g. 'debug://probe-rs?target=nrf52840', "
            "'debug://auto?target=esp32') or --sim for the in-memory simulator"
        )
    inst = connect(target, device_slug=getattr(args, "device", None), timeout=args.timeout)
    try:
        payload, human = _debug_dispatch(args.debug_cmd, args, inst)
    finally:
        inst.close()
    _emit(args, payload, human)
    return 0 if payload.get("ok", True) else 1


# -- atklogic (in-process ATK-Logic-Analyzer) ---------------------------------


def _parse_channels(spec: str) -> list[str]:
    """``'0'`` / ``'0,1,7'`` / ``'ch0,ch7'`` -> token list for ``channel_to_index``."""
    tokens = [t for t in str(spec or "").replace(" ", "").split(",") if t]
    return tokens or ["0"]


def _parse_pairs(spec: str) -> dict:
    """``'rx=0,tx=1'`` -> ``{'rx': 0, 'tx': 1}`` (a sigrok channel map)."""
    out: dict = {}
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            out[k] = int(v[2:]) if v.lower().startswith("ch") else int(v)
        except ValueError:
            pass
    return out


def _parse_options(specs) -> dict:
    """``['baudrate=115200']`` -> ``{'baudrate': 115200}`` (ints when possible)."""
    out: dict = {}
    for part in specs or []:
        part = str(part).strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            out[k] = int(v)
        except ValueError:
            out[k] = v
    return out


def _build_decoders(args: argparse.Namespace) -> list[dict]:
    """Assemble the decoder stack for ``hil atklogic decode`` (first gets map+opts)."""
    names = [n for n in (args.decoder or []) if n]
    if not names:
        return []
    stack = [{"name": names[0], "channels": _parse_pairs(getattr(args, "map", "")),
              "options": _parse_options(getattr(args, "option", []))}]
    stack += [{"name": n} for n in names[1:]]
    return stack


def _atklogic_dispatch(action: str, args: argparse.Namespace) -> tuple[dict, str]:
    """Run one ``hil atklogic`` action; return a ``(payload, human)`` pair."""
    from .drivers._atklogic import AtkCore

    core = AtkCore(serial=args.serial, decoders_dir=args.decoders, libusb=args.libusb)
    try:
        if action == "info":
            ident = core.identify()
            human = "\n".join([
                f"{ident['vendor']} {ident['model']}  SN {ident['serial'] or '-'}",
                f"  usb_id:    {ident['usb_id']}",
                f"  channels:  {ident['channels']}",
                f"  rates_mhz: {ident['rates_mhz']}",
                f"  max_rate:  {ident['max_sample_rate_hz']} Hz",
                f"  libusb:    {ident['libusb'] or '(system)'}",
                f"  decoders:  {ident['decoders_dir']}",
            ])
            return {"ok": True, **ident}, human

        if action == "list":
            protos = core.list_protocols(args.filter or "")
            head = f"{len(protos)} decoder(s) in {core.decoders_dir}" + (":" if protos else "")
            human = "\n".join([head] + [f"  {n}" for n in protos])
            return {"ok": True, "count": len(protos), "decoders_dir": core.decoders_dir,
                    "filter": args.filter or "", "protocols": protos}, human

        if action == "capture":
            cap = core.capture(channels=_parse_channels(args.channels), depth=args.depth,
                               sample_rate_hz=args.rate, threshold_v=args.threshold,
                               trigger=args.trigger, mode=args.mode,
                               timeout_s=max(args.timeout, 30.0), rle=args.rle)
            out = core.save_capture(cap, args.fmt, args.out)
            stats = {c: cap.stats(c) for c in cap.chs}
            lines = [
                f"captured {cap.depth} samples @ {cap.srate_hz:g} Hz "
                f"({'complete' if cap.complete else 'partial'}) "
                f"in {cap.elapsed_s:.3f}s -> {out}",
                f"  channels: {cap.chs}  trigger: {cap.trigger}  mode: {cap.mode}  fmt: {args.fmt}",
            ]
            for c in cap.chs:
                s = stats[c]
                lines.append(
                    f"  ch{c}: high={(s.get('high_pct') or 0):.1f}% "
                    f"rising={s.get('rising_edges') or 0} falling={s.get('falling_edges') or 0} "
                    f"freq={(s.get('frequency_hz') or 0):.6g}Hz")
            return {"ok": True, "path": str(out), "format": args.fmt, "channels": cap.chs,
                    "sample_rate_hz": cap.srate_hz, "sample_count": cap.depth,
                    "duration_s": cap.duration_s, "complete": cap.complete,
                    "elapsed_s": cap.elapsed_s,
                    "bytes": out.stat().st_size if out.exists() else 0,
                    "trigger": cap.trigger, "mode": cap.mode, "stats": stats}, "\n".join(lines)

        if action == "decode":
            result = core.decode(args.artifact, _build_decoders(args) or None,
                                 max_samples=args.max_samples, max_items=args.max_items)
            payload = {"ok": True, "source": args.artifact, "chain": result.get("chain"),
                       "frames": result.get("frames"), "from_saved": result.get("from_saved"),
                       "sample_rate_hz": result.get("sample_rate_hz"),
                       "layers": result.get("layers"), "transactions": result.get("transactions")}
            lines = [f"decoded {result.get('frames', 0)} frame(s) from {args.artifact}",
                     f"  chain: {result.get('chain')}"]
            if args.out:
                out, frames = core.write_decode(result, args.fmt, args.out)
                payload.update({"out": str(out), "out_format": args.fmt, "out_frames": frames,
                                "bytes": out.stat().st_size if out.exists() else 0})
                lines.append(f"  wrote {frames} row(s) -> {out}")
            return payload, "\n".join(lines)

        if action == "convert":
            channels = _parse_channels(args.channels) if args.channels else None
            res = core.convert(args.artifact, args.fmt, args.out, channels=channels)
            human = (f"converted {args.artifact} -> {res['path']} ({res['format']}, "
                     f"{res['sample_count']} samples, ch {res['channels']} "
                     f"@ {res['sample_rate_hz']:g} Hz, {res['bytes']} B)")
            return {"ok": True, **res}, human

        raise HilError(f"hil atklogic: unknown action '{action}'")
    finally:
        core.close()


def cmd_atklogic(args: argparse.Namespace) -> int:
    payload, human = _atklogic_dispatch(args.atklogic_cmd, args)
    _emit(args, payload, human)
    return 0 if payload.get("ok", True) else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    import platform

    from . import provision, pyinstall
    from .transports import backend_details

    devices = load_registry()
    prov = provision.status(platform=provision.detect())
    payload = {
        "hil_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "backends": backend_status(),
        "backend_details": backend_details(),
        "environment": pyinstall.environment_report(),
        "registry_devices": len(devices),
        "provisioning": [
            {"slug": r.recipe.slug, "ready": r.satisfied,
             "license": r.recipe.license,
             "hint": "" if r.satisfied else provision.setup_hint(r.recipe.slug)}
            for r in prov
        ],
    }
    if args.json:
        _emit(args, payload, "")
    else:
        backends = "  ".join(
            f"{name}: {'ok' if ok else 'MISSING'}"
            for name, ok in payload["backends"].items()
        )
        prov_str = "  ".join(
            f"{p['slug']}: {'ready' if p['ready'] else p['hint']}"
            for p in payload["provisioning"]
        ) or "(none)"
        env = payload["environment"]
        lines = [
            f"hil {__version__} on Python {payload['python']} ({payload['platform']})",
            f"interpreter: {env['python']}"
            + ("  (uv tool venv — no pip)" if env["uv_tool_venv"] else ""),
            f"backends: {backends}",
            f"registry: {payload['registry_devices']} devices",
            f"optional device backends: {prov_str}",
        ]
        # Concrete, copy-pasteable install commands for any missing python backend.
        missing = [b for b in ("serial", "visa") if not payload["backends"].get(b)]
        if not payload["backends"].get("usb") and "visa" not in missing:
            missing.append("usb")
        for b in dict.fromkeys(missing):
            lines.append(f"  to enable {b}: {pyinstall.install_command(pyinstall.backend_packages(b))}")
        lines.append(
            "  all backends at once: "
            + pyinstall.install_command(pyinstall.backend_packages("all"))
        )
        lines.append("missing device backend? run the hinted 'hil setup <slug>' (or 'hil setup --list')")
        _emit(args, {}, "\n".join(lines))
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """`hil setup [targets...]` — check / download / build device backends."""
    from . import provision, pyinstall

    platform = provision.detect()

    if args.list:
        recipes = provision.load_recipes()
        rows = [r.to_dict() for r in recipes.values()]
        backends = {
            name: {"packages": list(pyinstall.backend_packages(name)),
                   "label": pyinstall.BACKEND_LABELS.get(name, ""),
                   "command": pyinstall.install_command(pyinstall.backend_packages(name))}
            for name in ("serial", "visa", "usb", "mdns", "all")
        }
        if args.json:
            _emit(args, {"recipes": rows, "python_backends": backends}, "")
        else:
            lines = ["python backends (installed into this interpreter):"]
            for name, b in backends.items():
                lines.append(f"  {name:<7} {b['label']}")
                lines.append(f"          {b['command']}")
            lines.append("provisioning recipes (third-party device backends):")
            for slug, r in sorted(recipes.items()):
                lines.append(f"  {slug}  [{r.license or 'n/a'}]  driver={r.driver or '-'}")
                if r.description:
                    lines.append(f"      {r.description.strip()}")
            lines.append("run: hil setup <backend|slug> [--dry-run|--yes]   |   hil setup <device-slug>")
            _emit(args, {}, "\n".join(lines))
        return 0

    targets = list(args.targets or [])

    # No targets, or --check: report status only (never changes anything).
    if args.check or not targets:
        reports = provision.status(targets or None, platform=platform)
        if args.json:
            _emit(args, [r.to_dict() for r in reports], "")
        else:
            lines = [f"host: {platform.describe()}"]
            for r in reports:
                mark = "ready" if r.satisfied else "NOT provisioned"
                lines.append(f"  {r.recipe.slug}: {mark}")
                for d in r.detect:
                    lines.append(f"      [{'ok' if d.ok else ' missing'}] {d.probe.label()}"
                                 + (f" — {d.detail}" if d.detail and not d.ok else ""))
                if not r.satisfied:
                    lines.append(f"      -> {provision.setup_hint(r.recipe.slug)}")
            _emit(args, {}, "\n".join(lines))
        return 0 if all(r.satisfied for r in reports) else 1

    # Python-backend extras (serial/visa/usb/mdns/all) install PyPI packages into
    # THIS interpreter — they are not third-party device recipes, so handle them
    # here with the uv-tool-aware command from hil.pyinstall.
    from . import pyinstall

    backend_targets = [t for t in targets if pyinstall.is_backend(t)]
    if backend_targets:
        return _setup_python_backends(args, backend_targets)

    # Provision the resolved targets.
    reports = provision.provision(
        targets,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        platform=platform,
        on_output=(lambda _line: None) if args.json else print,
    )
    if args.json:
        _emit(args, [r.to_dict() for r in reports], "")
    else:
        for r in reports:
            print(r.human())
            if r.recipe.notes and not r.satisfied:
                print(f"  notes: {r.recipe.notes.strip()}")
    return 0 if all(r.ok for r in reports) else 1


def _setup_python_backends(args: argparse.Namespace, names: list[str]) -> int:
    """Install python-backend packages (serial/visa/usb/mdns/all) into THIS env.

    Uses the same uv-tool-aware command the provisioning engine does, so it works
    in a uv venv / uv tool install that has no ``pip``. Non-privileged (a package
    install), so it runs unless ``--dry-run``; ``--dry-run`` just prints it.
    """
    import shlex
    import subprocess

    from . import pyinstall

    pkgs: list[str] = []
    for n in names:
        for p in pyinstall.backend_packages(n):
            if p not in pkgs:
                pkgs.append(p)
    cmd = pyinstall.install_command(pkgs)
    argv = shlex.split(cmd)

    if args.dry_run:
        payload = {"ok": True, "dry_run": True, "backends": names,
                   "packages": pkgs, "command": cmd}
        _emit(args, payload, f"would run: {cmd}")
        return 0

    if not args.json:
        print(f"installing {', '.join(pkgs)} into {pyinstall.running_python()}")
        print(f"  $ {cmd}")
    try:
        proc = subprocess.run(argv, capture_output=args.json, text=True)
        ok = proc.returncode == 0
    except FileNotFoundError as exc:
        payload = {"ok": False, "backends": names, "packages": pkgs, "command": cmd,
                   "error": f"installer not runnable: {exc}",
                   "hint": "install uv (https://docs.astral.sh/uv/) or run the command above manually."}
        _emit(args, payload, f"error: {payload['error']}")
        return 1

    payload = {"ok": ok, "backends": names, "packages": pkgs, "command": cmd,
               "returncode": proc.returncode}
    if args.json:
        if proc.stdout:
            payload["stdout"] = proc.stdout[-4000:]
        if proc.stderr:
            payload["stderr"] = proc.stderr[-4000:]
        _emit(args, payload, "")
    else:
        human = ("installed: " + ", ".join(pkgs)) if ok else (
            f"install failed (exit {proc.returncode}) — see output above")
        _emit(args, {}, human)
    return 0 if ok else 1


def _log_instrument(args: argparse.Namespace):
    """Build the instrument for `hil log` (real target, or simulator dry-run)."""
    if args.sim:
        from .bench.sim import DEFAULT_IDN, SimTransport
        from .scpi import ScpiInstrument

        transport = SimTransport(
            responses={args.channel: str(args.sim_value)},
            idn=DEFAULT_IDN,
            default_query_reply=str(args.sim_value),
            timeout=args.timeout,
        )
        transport.open()
        device = load_registry().get(args.device) if args.device else None
        return ScpiInstrument(transport, device=device, timeout=args.timeout)
    return connect(args.address, device_slug=args.device, timeout=args.timeout)


def cmd_log(args: argparse.Namespace) -> int:
    inst = _log_instrument(args)
    try:
        for cmd in getattr(args, "setup", None) or []:
            r = inst.safe_execute(cmd)
            if not r.get("ok", True):
                raise HilError(
                    f"setup command failed: {cmd!r} -> "
                    f"{r.get('error') or '; '.join(r.get('errors') or []) or 'error'}"
                )
        samples = inst.capture(args.channel, args.seconds, args.interval, args.units)
    finally:
        inst.close()
    stats = summarize(samples, args.units)
    out = None
    if args.out:
        with DataLogger(args.out) as log:
            log.writemany(samples)
        out = str(args.out)
    payload = {
        "ok": True,
        "channel": args.channel,
        "captured": len(samples),
        "file": out,
        "stats": stats,
    }
    human = "\n".join(
        [
            f"captured {len(samples)} samples on '{args.channel}' "
            f"over {stats.get('duration_s', 0)}s" + (f" -> {out}" if out else ""),
            f"  mean={stats.get('mean')} {args.units}  min={stats.get('min')}  "
            f"max={stats.get('max')}  rate={stats.get('rate_hz')} Hz",
        ]
    )
    _emit(args, payload, human)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    samples = read_samples(args.file)
    groups = list(iter_channels(samples))
    payload = {
        "ok": True,
        "file": args.file,
        "count": len(samples),
        "channels": {ch: summarize(ss) for ch, ss in groups},
    }
    if args.json:
        _emit(args, payload, "")
    elif args.md:
        lines = [
            f"# Analysis — {args.file}",
            "",
            "| channel | n | mean | min | max | p95 | rate (Hz) | units |",
            "|---|--:|--:|--:|--:|--:|--:|---|",
        ]
        for ch, ss in groups:
            s = summarize(ss)
            lines.append(
                f"| {ch} | {s['count']} | {s['mean']:.6g} | {s['min']:.6g} "
                f"| {s['max']:.6g} | {s['p95']:.6g} | {s['rate_hz']} | {s['units']} |"
            )
        print("\n".join(lines))
    else:
        lines = [f"{args.file}: {len(samples)} samples, {len(groups)} channel(s)"]
        for ch, ss in groups:
            s = summarize(ss)
            extra = f"  energy={s['energy_wh']} Wh" if "energy_wh" in s else ""
            lines.append(
                f"  {ch}: n={s['count']} mean={s['mean']:.6g} min={s['min']:.6g} "
                f"max={s['max']:.6g} p95={s['p95']:.6g} rate={s['rate_hz']}Hz {s['units']}{extra}"
            )
        _emit(args, {}, "\n".join(lines))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """`hil mcp [install|config]` — serve HIL over MCP and/or register it globally."""
    from . import mcp_server

    subcmd = getattr(args, "mcp_cmd", None)
    include_path = not getattr(args, "no_path", False)

    if subcmd == "config":
        print(mcp_server.config_snippet(
            agent=getattr(args, "agent", ""), include_path=include_path))
        return 0

    if subcmd == "install":
        report = mcp_server.install(
            agent=getattr(args, "agent", ""),
            dry_run=getattr(args, "dry_run", False),
            include_path=include_path,
        )
        ok = report.get("ok", True)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
            return 0 if ok else 1
        targets = report.get("targets") or []
        if not targets:
            print(report.get("note") or "no supported agent config detected")
            return 0
        verb = "would register" if report.get("dry_run") else "registered"
        for t in targets:
            if not t.get("ok"):
                print(f"  [skip] {t['agent']}: {t.get('error')}")
            else:
                print(f"  {verb} 'hil' in {t['agent']}  ({t.get('changed')})  ->  {t['path']}")
        print("\nRestart the agent to load the HIL tools. Prefer to paste it yourself? "
              "Run `hil mcp config`.")
        return 0 if ok else 1

    # No subcommand: run the server (stdio by default; loopback http/sse optional).
    mcp_server.run(transport=args.transport, host=args.host, port=args.port)
    return 0


_COMMANDS = {
    "probe": cmd_probe,
    "idn": cmd_idn,
    "exec": cmd_exec,
    "errors": cmd_errors,
    "devices": cmd_devices,
    "help": cmd_help,
    "discover": cmd_discover,
    "scrape": cmd_scrape,
    "bench": cmd_bench,
    "debug": cmd_debug,
    "atklogic": cmd_atklogic,
    "log": cmd_log,
    "analyze": cmd_analyze,
    "doctor": cmd_doctor,
    "setup": cmd_setup,
    "mcp": cmd_mcp,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except HilError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())
