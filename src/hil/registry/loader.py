"""YAML device registry loader.

Built-in definitions ship in ``hil/devices/``. Extra dirs (e.g. a lab's
private instruments) can be merged in; user definitions override built-ins
on slug collision.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..errors import RegistryError
from .schema import Command, Device, SafetyPolicy

BUILTIN_DIR = Path(__file__).parent.parent / "devices"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_REQUIRED = ("slug", "manufacturer", "model")


def load_device_file(path: Path) -> Device:
    """Parse and validate one device YAML file."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{path.name}: expected a mapping at top level.")

    missing = [k for k in _REQUIRED if not data.get(k)]
    if missing:
        raise RegistryError(f"{path.name}: missing required field(s): {', '.join(missing)}.")
    if not SLUG_RE.match(str(data["slug"])):
        raise RegistryError(
            f"{path.name}: slug '{data['slug']}' must be lowercase letters/digits/hyphens."
        )

    safety_raw = data.get("safety") or {}
    safety = SafetyPolicy(
        blocked=tuple(safety_raw.get("blocked", ())),
        allow=tuple(safety_raw.get("allow", ())),
        limits=dict(safety_raw.get("limits", {})),
    )
    for pattern in (*safety.blocked, *safety.allow):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RegistryError(
                f"{path.name}: invalid safety regex '{pattern}': {exc}"
            ) from exc

    commands = tuple(
        Command(
            name=str(c.get("name", "")),
            syntax=str(c.get("syntax", "")),
            description=str(c.get("description", "")),
            subsystem=str(c.get("subsystem", "")),
            query=bool(c.get("query", c.get("syntax", "").endswith("?"))),
            parameters=str(c.get("parameters", "")),
            param_specs=tuple(
                dict(p) for p in c.get("param_specs", ()) if isinstance(p, dict)
            ),
            returns=(dict(c["returns"]) if isinstance(c.get("returns"), dict) else None),
            examples=tuple(str(e) for e in c.get("examples", ())),
            section=str(c.get("section", "")),
            page=(int(c["page"]) if isinstance(c.get("page"), int) else None),
            anchor=str(c.get("anchor", "")),
        )
        for c in data.get("commands", ())
        if isinstance(c, dict)
    )

    return Device(
        slug=str(data["slug"]),
        manufacturer=str(data["manufacturer"]),
        model=str(data["model"]),
        category=str(data.get("category", "")),
        driver=str(data.get("driver", "scpi") or "scpi"),
        series=str(data.get("series", "")),
        description=str(data.get("description", "")),
        manual=str(data.get("manual", "")),
        date_modified=str(data.get("date_modified", "")),
        interfaces=tuple(str(i) for i in data.get("interfaces", ())),
        connection_examples=tuple(str(c) for c in data.get("connection_examples", ())),
        default_address=str(data.get("default_address", "")),
        capabilities=tuple(str(c) for c in data.get("capabilities", ())),
        safety=safety,
        commands=commands,
        sources=tuple(str(s) for s in data.get("sources", ())),
    )


def load_registry(extra_dirs: tuple[Path, ...] = ()) -> dict[str, Device]:
    """Load built-in devices plus any extra dirs (later wins on slug clash)."""
    devices: dict[str, Device] = {}
    for directory in (BUILTIN_DIR, *extra_dirs):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.y*ml")):
            device = load_device_file(path)
            devices[device.slug] = device
    return devices
