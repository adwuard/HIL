"""Bench procedure model: load, normalise and validate declarative test specs.

A *procedure* is a YAML/JSON file describing a scripted HIL test: which device
to drive, initial variables, an optional simulator profile, and an ordered list
of steps (write / query / measure / assert / delay / ...). Steps are written in
a compact shorthand so both humans and agents can author them quickly::

    name: SMU output accuracy
    device: keithley-2400
    sim:
      responses: {":MEAS:VOLT?": "3.301"}
    variables: {vset: 3.3}
    steps:
      - reset
      - write: ":SOUR:VOLT ${vset}"
      - measure: {command: ":MEAS:VOLT?", save_as: vout, units: V}
      - assert: {that: vout, op: within, value: "3.267..3.333"}
      - errors: {expect: none}

Validation is strict and collects *every* problem (unknown step, missing arg,
bad op) so an agent gets one actionable message rather than failing step by
step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import ProcedureError
from .assertions import OPS

# step type -> required args, optional args, and the shorthand "primary" arg
# used when a step is written as ``{type: scalar}`` instead of ``{type: {...}}``.
STEP_SPECS: dict[str, dict[str, Any]] = {
    "reset":   {"required": [], "optional": [], "primary": None},
    "opc":     {"required": [], "optional": ["save_as"], "primary": None},
    "idn":     {"required": [], "optional": ["save_as"], "primary": None},
    "write":   {"required": ["command"], "optional": [], "primary": "command"},
    "query":   {"required": ["command"], "optional": ["save_as"], "primary": "command"},
    "measure": {"required": ["command"], "optional": ["save_as", "units", "parse"],
                "primary": "command"},
    "assert":  {"required": ["that", "op"], "optional": ["value", "message"],
                "primary": None},
    "errors":  {"required": [], "optional": ["expect", "save_as"], "primary": None},
    "delay":   {"required": ["seconds"], "optional": [], "primary": "seconds"},
    "wait":    {"required": [], "optional": ["timeout"], "primary": None},
    "set":     {"required": ["var", "value"], "optional": [], "primary": None},
    "log":     {"required": ["message"], "optional": [], "primary": "message"},
}
STEP_TYPES = tuple(STEP_SPECS)

# A ready-to-run example written by `hil bench init`.
SAMPLE_SPEC = """\
# HIL bench procedure — run with:  hil bench run <this-file>
# No address + no --sim needed: without hardware this dry-runs on the simulator.
name: SMU output accuracy
description: Set 3.3 V, measure, and check the reading is within 1%.
device: keithley-2400          # registry slug -> safety policy + identity
variables:
  vset: 3.3
sim:                           # canned replies for hardware-free dry-runs
  idn: "KEITHLEY,2400,SN123456,C33"
  responses:
    ":MEAS:VOLT?": "3.301"
    ":MEAS:CURR?": "0.052"
steps:
  - reset
  - write: ":SOUR:FUNC VOLT"
  - write: ":SOUR:VOLT ${vset}"
  - opc
  - measure: {command: ":MEAS:VOLT?", save_as: vout, units: V}
  - assert: {that: vout, op: within, value: "3.267..3.333", message: "output within 1%"}
  - measure: {command: ":MEAS:CURR?", save_as: iout, units: A}
  - assert: {that: iout, op: lt, value: 1.0, message: "current under 1 A"}
  - errors: {expect: none}
"""


@dataclass
class Step:
    """One normalised bench step."""

    type: str
    args: dict[str, Any] = field(default_factory=dict)
    index: int = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)

    def summary(self) -> str:
        """A short human/agent-readable description of the step."""
        a = self.args
        if self.type in ("write", "query", "measure"):
            base = f"{self.type} {a.get('command', '')}"
            if a.get("save_as"):
                base += f" -> {a['save_as']}"
            if self.type == "measure" and a.get("units"):
                base += f" [{a['units']}]"
            return base
        if self.type == "assert":
            val = a.get("value", "")
            return f"assert {a.get('that', '')} {a.get('op', '')} {val}".rstrip()
        if self.type == "set":
            return f"set {a.get('var', '')} = {a.get('value', '')}"
        if self.type == "delay":
            return f"delay {a.get('seconds', 0)}s"
        if self.type == "wait":
            return f"wait (timeout {a.get('timeout', 'default')})"
        if self.type == "errors":
            return f"errors (expect {a.get('expect', 'any')})"
        if self.type == "log":
            return f"log {a.get('message', '')}"
        return self.type


@dataclass
class Sim:
    """Simulator profile for hardware-free dry-runs."""

    idn: str = ""
    responses: dict[str, str] = field(default_factory=dict)
    default_query_reply: str = "0"
    errors: list[str] = field(default_factory=list)


@dataclass
class Procedure:
    """A parsed, validated bench procedure."""

    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    device: str = ""
    address: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    sim: Sim = field(default_factory=Sim)
    continue_on_error: bool = False
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "device": self.device,
            "address": self.address,
            "variables": self.variables,
            "continue_on_error": self.continue_on_error,
            "steps": [{"type": s.type, "index": s.index, **s.args} for s in self.steps],
        }


def _normalize_step(raw: Any, index: int) -> tuple[Step | None, str | None]:
    """Turn one raw spec entry into a Step, or return an error string."""
    if isinstance(raw, str):
        return Step(type=raw.strip().lower(), args={}, index=index), None
    if not isinstance(raw, dict):
        return None, f"step {index}: expected a string or mapping, got {type(raw).__name__}"

    if "type" in raw:
        stype = str(raw["type"]).strip().lower()
        args = {k: v for k, v in raw.items() if k != "type"}
    elif len(raw) == 1:
        (key, value), = raw.items()
        stype = str(key).strip().lower()
        spec = STEP_SPECS.get(stype)
        if isinstance(value, dict):
            args = dict(value)
        elif spec and spec["primary"]:
            args = {spec["primary"]: value}
        elif value is None or spec is None:
            # Bare step, or an unknown type with a scalar: let _validate_step
            # report it clearly ("unknown type") rather than a shorthand error.
            args = {}
        else:
            return None, (
                f"step {index} ('{stype}'): shorthand value needs a "
                f"'{spec['primary']}' mapping"
            )
    else:
        return None, (
            f"step {index}: mapping must have a single step key or a 'type' "
            f"(got {sorted(raw)})"
        )
    return Step(type=stype, args=args, index=index), None


def _validate_step(step: Step) -> list[str]:
    errs: list[str] = []
    spec = STEP_SPECS.get(step.type)
    if spec is None:
        errs.append(
            f"step {step.index}: unknown type '{step.type}' "
            f"(known: {', '.join(STEP_TYPES)})"
        )
        return errs
    for req in spec["required"]:
        if req not in step.args or step.args[req] in (None, ""):
            errs.append(f"step {step.index} ('{step.type}'): missing '{req}'")
    allowed = set(spec["required"]) | set(spec["optional"])
    for key in step.args:
        if key not in allowed:
            errs.append(
                f"step {step.index} ('{step.type}'): unexpected arg '{key}' "
                f"(allowed: {', '.join(sorted(allowed)) or 'none'})"
            )
    if step.type == "assert":
        op = str(step.args.get("op", "")).lower()
        if op and op not in OPS:
            errs.append(
                f"step {step.index} ('assert'): unknown op '{op}' "
                f"(known: {', '.join(sorted(OPS))})"
            )
        if op in ("within", "approx", "eq", "ne", "lt", "le", "gt", "ge",
                  "contains", "not_contains", "matches", "not_matches") \
                and "value" not in step.args:
            errs.append(f"step {step.index} ('assert'): op '{op}' needs a 'value'")
    if step.type == "errors":
        expect = str(step.args.get("expect", "")).lower()
        if expect and expect not in ("none", "any"):
            errs.append(f"step {step.index} ('errors'): expect must be 'none' or 'any'")
    if step.type == "delay":
        if not isinstance(step.args.get("seconds"), (int, float)):
            errs.append(f"step {step.index} ('delay'): 'seconds' must be a number")
    return errs


def validate_procedure(data: Any) -> list[str]:
    """Return every validation problem in a raw spec dict (empty == valid)."""
    errs: list[str] = []
    if isinstance(data, list):
        meta: dict[str, Any] = {}
        steps_raw = data
    elif isinstance(data, dict):
        meta = data
        steps_raw = data.get("steps", [])
    else:
        return [f"spec must be a mapping or a list of steps, got {type(data).__name__}"]

    if not isinstance(steps_raw, list):
        return ["'steps' must be a list"]
    if not steps_raw:
        errs.append("procedure has no steps")

    variables = meta.get("variables", {})
    if variables and not isinstance(variables, dict):
        errs.append("'variables' must be a mapping")
    sim = meta.get("sim", {})
    if sim and not isinstance(sim, dict):
        errs.append("'sim' must be a mapping")
    elif isinstance(sim, dict):
        responses = sim.get("responses", {})
        if responses and not isinstance(responses, dict):
            errs.append("'sim.responses' must be a mapping of command -> reply")

    for i, raw in enumerate(steps_raw, 1):
        step, err = _normalize_step(raw, i)
        if err:
            errs.append(err)
        elif step is not None:
            errs.extend(_validate_step(step))
    return errs


def parse_procedure(data: Any, *, source: str = "") -> Procedure:
    """Build a :class:`Procedure` from a parsed spec; raise on any problem."""
    errs = validate_procedure(data)
    if errs:
        raise ProcedureError(
            "invalid procedure"
            + (f" ({source})" if source else "")
            + ":\n  - " + "\n  - ".join(errs)
        )
    if isinstance(data, list):
        meta, steps_raw = {}, data
    else:
        meta, steps_raw = data, data.get("steps", [])

    sim_raw = meta.get("sim", {}) or {}
    sim = Sim(
        idn=str(sim_raw.get("idn", "")),
        responses={str(k): str(v) for k, v in (sim_raw.get("responses", {}) or {}).items()},
        default_query_reply=str(sim_raw.get("default_query_reply", "0")),
        errors=[str(e) for e in (sim_raw.get("errors", []) or [])],
    )
    name = str(meta.get("name") or (Path(source).stem if source else "procedure"))
    steps: list[Step] = []
    for i, raw in enumerate(steps_raw, 1):
        step, _ = _normalize_step(raw, i)
        if step is not None:
            steps.append(step)
    return Procedure(
        name=name,
        steps=steps,
        description=str(meta.get("description", "")),
        device=str(meta.get("device", "")),
        address=str(meta.get("address", "")),
        variables=dict(meta.get("variables", {}) or {}),
        sim=sim,
        continue_on_error=bool(meta.get("continue_on_error", False)),
        source=source,
    )


def load_procedure(path: str | Path) -> Procedure:
    """Load a procedure from a YAML or JSON file."""
    p = Path(path)
    if not p.exists():
        raise ProcedureError(f"procedure file not found: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ProcedureError(f"could not parse {p}: {exc}") from exc
    if data is None:
        raise ProcedureError(f"empty procedure file: {p}")
    return parse_procedure(data, source=str(p))
