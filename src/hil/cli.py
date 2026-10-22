"""`hil` command-line interface — agent-first: every command speaks --json.

    hil probe tcp://192.168.1.5:5025          # is it alive? clear diagnostics
    hil idn keithley-2400                     # *IDN? via registry
    hil exec <addr> '*IDN?' 'MEAS:VOLT?'     # run commands; '?' = query
    hil errors <addr>                         # drain SYST:ERR?
    hil devices list | show <slug>            # registry
    hil log <addr> --channel :MEAS:CURR? --seconds 30 --out run.ndjson
    hil analyze run.ndjson                    # summarize a capture
    hil doctor                                # env + backend availability
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

    _add_scrape_parser(sub, sub_common)
    _add_bench_parser(sub, sub_common)
    _add_telemetry_parsers(sub, sub_common)

    sub.add_parser("doctor", help="environment and backend availability", parents=[sub_common])
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
    lp.add_argument("--sim", action="store_true", help="hardware-free dry-run on the simulator")
    lp.add_argument("--sim-value", default="1.0", help="simulated reply for --sim (default 1.0)")

    ap = sub.add_parser(
        "analyze", help="summarize a captured telemetry file", parents=[sub_common]
    )
    ap.add_argument("file", help="capture file (.ndjson/.jsonl/.csv)")
    ap.add_argument("--md", action="store_true", help="print a Markdown table")


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
            if r["response"] is not None:
                lines.append(f"{r['command']} -> {r['response']}")
            elif r["errors"]:
                lines.append(f"{r['command']} -> ERROR {r['errors']}")
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


def cmd_doctor(args: argparse.Namespace) -> int:
    import platform

    devices = load_registry()
    payload = {
        "hil_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "backends": backend_status(),
        "registry_devices": len(devices),
    }
    if args.json:
        _emit(args, payload, "")
    else:
        backends = "  ".join(
            f"{name}: {'ok' if ok else 'MISSING'}"
            for name, ok in payload["backends"].items()
        )
        _emit(
            args,
            {},
            "\n".join(
                [
                    f"hil {__version__} on Python {payload['python']} ({payload['platform']})",
                    f"backends: {backends}",
                    f"registry: {payload['registry_devices']} devices",
                    "missing backend? pip install 'hil-harness[serial]' / 'hil-harness[visa]' / 'hil-harness[all]'",
                ]
            ),
        )
    return 0


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


_COMMANDS = {
    "probe": cmd_probe,
    "idn": cmd_idn,
    "exec": cmd_exec,
    "errors": cmd_errors,
    "devices": cmd_devices,
    "scrape": cmd_scrape,
    "bench": cmd_bench,
    "log": cmd_log,
    "analyze": cmd_analyze,
    "doctor": cmd_doctor,
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
