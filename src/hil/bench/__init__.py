"""Agentic HIL test bench: declarative, safety-checked instrument procedures.

The bench runs a scripted *procedure* (YAML/JSON steps) against an instrument —
real hardware, or a built-in simulator for hardware-free dry-runs — and produces
a structured run report. It is the CLI/agent face of roadmap P4 (sequences,
measurement capture, run reports).

Public API::

    from hil.bench import load_procedure, run_procedure, resolve_run_target, \
        build_instrument_factory

    proc = load_procedure("bench/smu.yaml")
    mode, target, slug = resolve_run_target(proc, sim=False, address="", device=None)
    factory = build_instrument_factory(proc, mode=mode, target=target,
                                       device_slug=slug, timeout=5.0)
    report = run_procedure(proc, factory, mode=mode, target=target)
    print(report.human())
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..scpi import ScpiInstrument
from .assertions import OPS, evaluate
from .procedure import (
    SAMPLE_SPEC,
    STEP_TYPES,
    Procedure,
    Sim,
    Step,
    load_procedure,
    parse_procedure,
    validate_procedure,
)
from .report import Measurement, RunReport, StepResult
from .runner import run_procedure
from .sim import DEFAULT_IDN, SimTransport

__all__ = [
    "Procedure",
    "Step",
    "Sim",
    "load_procedure",
    "parse_procedure",
    "validate_procedure",
    "SAMPLE_SPEC",
    "STEP_TYPES",
    "evaluate",
    "OPS",
    "RunReport",
    "StepResult",
    "Measurement",
    "run_procedure",
    "SimTransport",
    "DEFAULT_IDN",
    "resolve_run_target",
    "build_instrument_factory",
]


def resolve_run_target(
    proc: Procedure,
    *,
    sim: bool = False,
    address: str = "",
    device: str | None = None,
) -> tuple[str, str, str]:
    """Decide how to run a procedure. Returns ``(mode, target, device_slug)``.

    Safety default: without an explicit address (CLI ``--address`` or the spec's
    ``address:``) the bench runs on the **simulator** — a dry-run never touches
    hardware by accident. ``--sim`` forces the simulator even with an address.
    """
    device_slug = device or proc.device or ""
    if sim:
        return "sim", "simulator", device_slug
    addr = address or proc.address
    if addr:
        return "live", addr, device_slug
    return "sim", "simulator", device_slug


def build_instrument_factory(
    proc: Procedure,
    *,
    mode: str,
    target: str,
    device_slug: str = "",
    timeout: float = 5.0,
    registry_dirs: tuple[Path, ...] = (),
) -> Callable[[], ScpiInstrument]:
    """Return a zero-arg factory that opens the instrument for a bench run.

    The factory is called once by :func:`~hil.bench.runner.run_procedure`; the
    runner closes the transport afterwards. Simulator runs still attach the
    device's safety policy (resolved from the registry) so blocked commands fail
    exactly as they would on hardware.
    """
    if mode == "live":
        from .. import connect

        def factory() -> ScpiInstrument:
            return connect(
                target,
                device_slug=device_slug or None,
                timeout=timeout,
                registry_dirs=registry_dirs,
            )

        return factory

    # simulator: resolve the safety policy (if any) up front
    device = None
    if device_slug:
        from .. import load_registry

        device = load_registry(registry_dirs).get(device_slug)

    def factory() -> ScpiInstrument:
        transport = SimTransport(
            responses=proc.sim.responses,
            idn=proc.sim.idn or DEFAULT_IDN,
            default_query_reply=proc.sim.default_query_reply,
            error_queue=list(proc.sim.errors),
            timeout=timeout,
        )
        transport.open()
        return ScpiInstrument(transport, device=device, timeout=timeout)

    return factory
