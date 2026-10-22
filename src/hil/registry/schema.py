"""Device definition schema (YAML-driven, validated on load).

One YAML file per device under ``hil/devices/`` (built-in) or a user dir.
The schema anticipates scraped reference data: interfaces, connection
examples, curated SCPI commands, and safety boundaries an agent must respect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import SafetyViolationError


@dataclass(frozen=True)
class SafetyPolicy:
    """Per-device safety boundaries for agent-issued commands.

    - ``blocked``: regexes; any match blocks the command outright.
    - ``allow``: if non-empty, only commands matching at least one regex pass
      (deny-by-default mode, e.g. for a shared lab instrument).
    """

    blocked: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    limits: dict[str, str] = field(default_factory=dict)

    def check(self, command: str) -> None:
        head = command.strip().split(" ")[0].rstrip(";").upper()
        for pattern in self.blocked:
            if re.fullmatch(pattern, head, flags=re.IGNORECASE):
                raise SafetyViolationError(
                    f"Command '{head}' is blocked by the device safety policy "
                    f"(pattern '{pattern}')."
                )
        if self.allow:
            for pattern in self.allow:
                if re.fullmatch(pattern, head, flags=re.IGNORECASE):
                    return
            raise SafetyViolationError(
                f"Command '{head}' is not on the device allow-list. "
                f"Allowed patterns: {', '.join(self.allow)}"
            )


@dataclass(frozen=True)
class Command:
    """One SCPI command (curated by hand or scraped from the reference corpus)."""

    name: str
    syntax: str
    description: str = ""
    subsystem: str = ""  # e.g. 'SOURce', 'MEASure'
    query: bool = False
    parameters: str = ""  # human-readable summary, e.g. '<voltage> in volts, 0..200'
    param_specs: tuple[dict[str, object], ...] = ()  # structured params (scraped)
    returns: dict[str, object] | None = None  # return spec (scraped)
    examples: tuple[str, ...] = ()  # full usage examples (scraped)
    section: str = ""  # manual section reference (scraped)
    page: int | None = None  # manual page number (scraped)
    anchor: str = ""  # on-page anchor at the reference site (scraped)


@dataclass(frozen=True)
class Device:
    """A registry entry describing one supported instrument model."""

    slug: str
    manufacturer: str
    model: str
    category: str = ""
    series: str = ""  # product family, e.g. 'DHO1000/DHO4000 Series' (scraped)
    description: str = ""
    manual: str = ""  # source programming guide, e.g. 'DHO Series Programming Guide'
    date_modified: str = ""  # upstream doc revision date (YYYY-MM-DD), scraped
    interfaces: tuple[str, ...] = ()  # e.g. ('gpib', 'rs-232')
    connection_examples: tuple[str, ...] = ()  # e.g. ('visa://GPIB::24::INSTR')
    default_address: str = ""  # registry-level default if the host has none
    capabilities: tuple[str, ...] = ()  # e.g. ('source-dc-voltage',)
    safety: SafetyPolicy = field(default_factory=SafetyPolicy)
    commands: tuple[Command, ...] = ()
    sources: tuple[str, ...] = ()  # URLs the definition was derived from

    def check_command(self, command: str) -> None:
        self.safety.check(command)
