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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..drivers.debug import DEBUG_ACTIONS
from ..errors import ProcedureError
from .assertions import OPS

# A ``${name}`` variable reference (whole-string), allowed wherever a number is
# expected so durations/counts can be driven from procedure variables.
_VAR_REF = re.compile(r"^\s*\$\{\w+\}\s*$")


def _numeric_or_var(value: Any) -> bool:
    """True for real numbers, numeric strings, or a whole ``${var}`` reference."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        s = value.strip()
        if _VAR_REF.match(s):
            return True
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def _int_or_hex_or_var(value: Any) -> bool:
    """True for ints, ``0x``/``0b``/octal strings, or a whole ``${var}`` reference.

    Memory/flash addresses are written in hex (``0x08000000``), which ``float()``
    rejects — so addresses/widths use this instead of :func:`_numeric_or_var`.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        s = value.strip()
        if _VAR_REF.match(s):
            return True
        try:
            int(s, 0)
            return True
        except ValueError:
            return False
    return False


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
    "capture": {"required": ["channel", "seconds"],
                "optional": ["interval", "units", "save_as", "out"], "primary": None},
    "repeat":  {"required": ["count", "steps"], "optional": [], "primary": None},
    # Logic-analyzer capture -> a file-backed artifact (not in-memory samples).
    # Needs a capture size: one of seconds/duration_s or depth/depth_samples.
    "logic":   {"required": [], "primary": None,
                "optional": ["channels", "rate", "sample_rate_hz", "depth",
                             "depth_samples", "seconds", "duration_s", "trigger",
                             "format", "mode", "timeout", "out", "save_as"]},
    # Protocol decode over an artifact -> a decoded artifact.
    "decode":  {"required": [], "primary": "decoder",
                "optional": ["decoder", "decoders", "input", "options",
                             "channels", "map", "format", "out", "save_as"]},
    # Build & flash & on-chip debug — the hardware-in-the-loop pipeline
    # (spec §5.4.5 Build & Flash, §5.11 build -> flash -> debug/run -> capture).
    # ``build`` records a firmware fingerprint (and may run a compile command);
    # ``flash`` programs it via the debug driver; ``debug`` drives the probe.
    "build":   {"required": [], "primary": "artifact",
                "optional": ["command", "artifact", "firmware", "version",
                             "cwd", "env", "save_as", "timeout"]},
    "flash":   {"required": ["firmware"], "primary": "firmware",
                "optional": ["verify", "erase", "chip", "target", "address",
                             "tool", "save_as", "timeout"]},
    "debug":   {"required": ["action"], "primary": "action",
                "optional": ["action", "address", "width", "count", "register",
                             "value", "script", "duration_s", "out", "halt",
                             "timeout_s", "match", "chip", "firmware", "save_as"]},
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
        if self.type == "capture":
            extra = f" -> {a['save_as']}" if a.get("save_as") else ""
            return f"capture {a.get('channel', '')} for {a.get('seconds', 0)}s{extra}"
        if self.type == "repeat":
            body = a.get("steps") or []
            n = len(body) if isinstance(body, list) else 0
            return f"repeat {a.get('count', 0)}x ({n} steps)"
        if self.type == "logic":
            size = a.get("seconds", a.get("duration_s"))
            depth = a.get("depth", a.get("depth_samples"))
            what = f"{size}s" if size else (f"{depth} samples" if depth else "")
            base = f"logic capture {what}".rstrip()
            if a.get("channels") is not None:
                base += f" ch={a['channels']}"
            if a.get("save_as"):
                base += f" -> {a['save_as']}"
            return base
        if self.type == "decode":
            name = a.get("decoder") or a.get("decoders")
            base = f"decode {name}"
            if a.get("save_as"):
                base += f" -> {a['save_as']}"
            return base
        if self.type == "build":
            what = a.get("artifact") or a.get("firmware") or a.get("command") or ""
            base = f"build {what}".rstrip()
            if a.get("version"):
                base += f" v={a['version']}"
            if a.get("save_as"):
                base += f" -> {a['save_as']}"
            return base
        if self.type == "flash":
            base = f"flash {a.get('firmware', '')}"
            flags = []
            if a.get("chip"):
                flags.append(f"chip={a['chip']}")
            if a.get("erase"):
                flags.append("erase")
            if a.get("verify") is False:
                flags.append("no-verify")
            if flags:
                base += f" ({', '.join(flags)})"
            if a.get("save_as"):
                base += f" -> {a['save_as']}"
            return base
        if self.type == "debug":
            extra = a.get("register") or a.get("address") or a.get("script") or ""
            base = f"debug {a.get('action', '')}" + (f" {extra}" if extra else "")
            if a.get("save_as"):
                base += f" -> {a['save_as']}"
            return base
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
    long_running: bool = False  # soak/loop procedure: excluded from fast sim tests
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "device": self.device,
            "address": self.address,
            "variables": self.variables,
            "continue_on_error": self.continue_on_error,
            "long_running": self.long_running,
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
    if step.type in ("delay", "capture"):
        if not _numeric_or_var(step.args.get("seconds")):
            errs.append(f"step {step.index} ('{step.type}'): 'seconds' must be a number")
    if step.type == "repeat":
        count = step.args.get("count")
        if isinstance(count, bool) or not _numeric_or_var(count):
            errs.append(
                f"step {step.index} ('repeat'): 'count' must be an integer or ${{var}}"
            )
        elif isinstance(count, int) and count < 0:
            errs.append(f"step {step.index} ('repeat'): 'count' must be >= 0")
        body = step.args.get("steps")
        if not isinstance(body, list) or not body:
            errs.append(f"step {step.index} ('repeat'): 'steps' must be a non-empty list")
        else:
            for j, raw in enumerate(body, 1):
                sub, err = _normalize_step(raw, j)
                if err:
                    errs.append(f"step {step.index} ('repeat') -> {err}")
                elif sub is not None:
                    for e in _validate_step(sub):
                        errs.append(f"step {step.index} ('repeat') -> {e}")
    if step.type == "logic":
        a = step.args
        has_size = (_numeric_or_var(a.get("seconds")) or _numeric_or_var(a.get("duration_s"))
                    or _numeric_or_var(a.get("depth")) or _numeric_or_var(a.get("depth_samples")))
        if not has_size:
            errs.append(
                f"step {step.index} ('logic'): needs a capture size — one of "
                "'seconds', 'duration_s', 'depth' or 'depth_samples'"
            )
        for key in ("seconds", "duration_s", "rate", "sample_rate_hz",
                    "depth", "depth_samples", "timeout"):
            if key in a and not _numeric_or_var(a.get(key)):
                errs.append(f"step {step.index} ('logic'): '{key}' must be a number or ${{var}}")
        trig = a.get("trigger")
        if trig is not None and not isinstance(trig, dict):
            errs.append(f"step {step.index} ('logic'): 'trigger' must be a mapping")
    if step.type == "decode":
        a = step.args
        if not a.get("decoder") and not a.get("decoders"):
            errs.append(
                f"step {step.index} ('decode'): needs 'decoder' (a name) or "
                "'decoders' (a list)"
            )
        decs = a.get("decoders")
        if decs is not None and not isinstance(decs, (list, dict, str)):
            errs.append(f"step {step.index} ('decode'): 'decoders' must be a name, list or mapping")
    if step.type == "build":
        a = step.args
        if not (a.get("command") or a.get("artifact") or a.get("firmware")):
            errs.append(
                f"step {step.index} ('build'): needs a 'command' to run and/or an "
                "'artifact' (firmware path) to fingerprint"
            )
        if "timeout" in a and not _numeric_or_var(a.get("timeout")):
            errs.append(f"step {step.index} ('build'): 'timeout' must be a number or ${{var}}")
        if a.get("env") is not None and not isinstance(a.get("env"), dict):
            errs.append(f"step {step.index} ('build'): 'env' must be a mapping")
    if step.type == "flash":
        a = step.args
        if "address" in a and not _int_or_hex_or_var(a.get("address")):
            errs.append(
                f"step {step.index} ('flash'): 'address' must be a number/hex "
                "(e.g. 0x08000000) or ${{var}}"
            )
        if "timeout" in a and not _numeric_or_var(a.get("timeout")):
            errs.append(f"step {step.index} ('flash'): 'timeout' must be a number or ${{var}}")
    if step.type == "debug":
        a = step.args
        action = str(a.get("action", "")).lower().strip()
        if action and action not in DEBUG_ACTIONS:
            errs.append(
                f"step {step.index} ('debug'): unknown action '{action}' "
                f"(known: {', '.join(DEBUG_ACTIONS)})"
            )
        if action in ("read-mem", "write-mem") and "address" not in a:
            errs.append(f"step {step.index} ('debug'): action '{action}' needs an 'address'")
        if action in ("read-reg", "write-reg") and not a.get("register"):
            errs.append(f"step {step.index} ('debug'): action '{action}' needs a 'register'")
        if action in ("write-mem", "write-reg") and a.get("value") in (None, ""):
            errs.append(f"step {step.index} ('debug'): action '{action}' needs a 'value'")
        if action == "gdb" and not a.get("script"):
            errs.append(f"step {step.index} ('debug'): action 'gdb' needs a 'script'")
        if "address" in a and not _int_or_hex_or_var(a.get("address")):
            errs.append(f"step {step.index} ('debug'): 'address' must be a number/hex or ${{var}}")
        for key in ("width", "count", "duration_s", "timeout_s"):
            if key in a and not _numeric_or_var(a.get(key)):
                errs.append(f"step {step.index} ('debug'): '{key}' must be a number or ${{var}}")
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
        long_running=bool(meta.get("long_running", False)),
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
