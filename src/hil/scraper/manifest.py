"""Render scraped devices to registry YAML / JSON and build the manifest.

Two artefacts per device:

* **YAML** -- registry-compatible (loads via :func:`hil.registry.load_device_file`)
  so a scraped instrument becomes immediately usable by the harness. When a
  hand-curated seed already exists for the slug, its *safety policy*,
  interfaces, connection examples and capabilities are preserved and only the
  (complete) command set + metadata are filled in. This is the "existing data
  is incomplete" fix: keep the human safety curation, replace the stub
  commands with all of them.
* **JSON** -- lossless dump of everything scraped (every parameter, range,
  enum, default, unit, return spec, example, manual section/page).

The **manifest** is a multi-level roll-up: catalog totals -> per-vendor and
per-category groupings -> per-device stats and file pointers.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from .. import __version__
from ..registry.schema import Device
from .reference import INDEX_URL, CatalogEntry, ScrapedCommand, ScrapedDevice

_NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")

# Bumped when the manifest's shape changes (keys/levels), not when data does.
MANIFEST_SCHEMA_VERSION = "1.0"


# -- command rendering --------------------------------------------------------


def command_name(cmd: ScrapedCommand) -> str:
    """A stable, readable ``name`` for a scraped command.

    Prefers the on-page ``anchor`` (already unique per page); otherwise
    derives one from the mnemonic.
    """
    base = cmd.anchor.strip()
    if not base:
        base = cmd.mnemonic.lstrip(":*")
        base = base.replace("?", "_query")
    name = _NON_IDENTIFIER.sub("_", base.lower()).strip("_")
    return name or "command"


def summarize_params(cmd: ScrapedCommand) -> str:
    """Human-readable parameter summary, e.g. ``<bool: 1|ON|0|OFF>``."""
    parts: list[str] = []
    for p in cmd.parameters:
        label = p.name or "arg"
        detail = ""
        if p.enum_values:
            detail = "|".join(p.enum_values)
        elif p.range_min is not None or p.range_max is not None:
            lo = "" if p.range_min is None else p.range_min
            hi = "" if p.range_max is None else p.range_max
            detail = f"{lo}..{hi}"
            if p.unit:
                detail += f" {p.unit}"
        elif p.unit:
            detail = p.unit
        opt = "?" if p.optional else ""
        parts.append(f"<{label}{opt}: {detail}>" if detail else f"<{label}{opt}>")
    return " ".join(parts)


def command_to_yaml_dict(cmd: ScrapedCommand) -> "OrderedDict[str, Any]":
    d: "OrderedDict[str, Any]" = OrderedDict()
    d["name"] = command_name(cmd)
    d["syntax"] = cmd.mnemonic
    if cmd.is_query:
        d["query"] = True
    if cmd.subsystem:
        d["subsystem"] = cmd.subsystem
    if cmd.description:
        d["description"] = cmd.description
    summary = summarize_params(cmd)
    if summary:
        d["parameters"] = summary
    if cmd.parameters:
        d["param_specs"] = [
            _compact(p.to_dict()) for p in cmd.parameters
        ]
    if cmd.returns:
        d["returns"] = _compact(cmd.returns.to_dict())
    if cmd.examples:
        d["examples"] = list(cmd.examples)
    if cmd.section:
        d["section"] = cmd.section
    if cmd.page is not None:
        d["page"] = cmd.page
    if cmd.anchor:
        d["anchor"] = cmd.anchor
    return d


def _compact(d: dict[str, Any]) -> "OrderedDict[str, Any]":
    """Drop null/empty/default values so YAML stays readable.

    ``0`` is preserved (a real range bound); only ``None``/``[]``/``""`` and
    ``False`` (e.g. ``optional: false``, the default) are omitted.
    """
    out: "OrderedDict[str, Any]" = OrderedDict()
    for k, v in d.items():
        if v is None or v is False or v == [] or v == "":
            continue
        out[k] = v
    return out


# -- device rendering ---------------------------------------------------------


def device_to_yaml_dict(
    scraped: ScrapedDevice, base: Device | None = None
) -> "OrderedDict[str, Any]":
    """Registry-compatible ordered mapping for one scraped device.

    ``base`` (a curated seed already in the registry) contributes safety,
    interfaces, connection examples, capabilities, default address and
    category; everything else comes from the scrape.
    """
    d: "OrderedDict[str, Any]" = OrderedDict()
    d["slug"] = scraped.slug
    d["manufacturer"] = scraped.manufacturer or (base.manufacturer if base else "")
    d["model"] = scraped.model or (base.model if base else "")
    category = (base.category if base and base.category else scraped.category) or ""
    if category:
        d["category"] = category
    if scraped.series:
        d["series"] = scraped.series
    description = scraped.description or (base.description if base else "")
    if description:
        d["description"] = description
    if scraped.manual:
        d["manual"] = scraped.manual
    if scraped.date_modified:
        d["date_modified"] = scraped.date_modified

    interfaces = list(base.interfaces) if base and base.interfaces else []
    if interfaces:
        d["interfaces"] = interfaces
    conn = list(base.connection_examples) if base and base.connection_examples else []
    if conn:
        d["connection_examples"] = conn
    if base and base.default_address:
        d["default_address"] = base.default_address
    caps = list(base.capabilities) if base and base.capabilities else []
    if caps:
        d["capabilities"] = caps

    # safety is human-curated only -- never invent one from a scrape
    if base and (base.safety.blocked or base.safety.allow or base.safety.limits):
        safety: "OrderedDict[str, Any]" = OrderedDict()
        if base.safety.blocked:
            safety["blocked"] = list(base.safety.blocked)
        if base.safety.allow:
            safety["allow"] = list(base.safety.allow)
        if base.safety.limits:
            safety["limits"] = dict(base.safety.limits)
        d["safety"] = safety
    else:
        d["safety"] = {}

    d["commands"] = [command_to_yaml_dict(c) for c in scraped.commands]

    sources = list(base.sources) if base and base.sources else []
    if scraped.url and scraped.url not in sources:
        sources.append(scraped.url)
    if sources:
        d["sources"] = sources
    return d


def render_device_yaml(scraped: ScrapedDevice, base: Device | None = None) -> str:
    header = (
        f"# {scraped.name or scraped.slug} — from the reference corpus\n"
        f"# {scraped.command_count} commands across {scraped.subsystem_count} "
        f"subsystems ({scraped.query_count} queries, {scraped.setter_count} setters).\n"
        f"# Source: {scraped.url}\n"
        f"# NOTE: commands are machine-scraped; safety policy is "
        f"{'preserved from the curated seed' if base else 'empty (add before use)'}.\n"
    )
    body = yaml.safe_dump(
        _to_plain(device_to_yaml_dict(scraped, base)),
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )
    return header + body


def _to_plain(obj: Any) -> Any:
    """OrderedDict -> dict recursively (yaml dumps OrderedDict fine, but this
    keeps output stable across PyYAML versions)."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


# -- manifest -----------------------------------------------------------------


def build_manifest(
    devices: Iterable[ScrapedDevice],
    catalog: Iterable[CatalogEntry] = (),
    *,
    source: str = "",
    generated_at: str | None = None,
    dataset_version: str = "",
) -> dict[str, Any]:
    """Roll scraped devices (and optionally the full catalog) into a manifest.

    Levels: catalog totals -> vendors -> categories -> per-device stats.
    Devices that were only discovered (in ``catalog``) but not scraped still
    appear under ``devices`` with ``scraped: false`` so the manifest is a
    complete index of the site.
    """
    devices = list(devices)
    catalog = list(catalog)
    by_slug = {d.slug: d for d in devices}
    # The catalog is the authoritative source for category (device pages only
    # carry the site-wide articleSection), so backfill it for scraped devices.
    category_by_slug = {e.slug: e.category for e in catalog if e.category}

    vendors: dict[str, dict[str, Any]] = {}
    categories: dict[str, dict[str, Any]] = {}
    device_rows: list[dict[str, Any]] = []

    total_commands = total_queries = 0
    for d in devices:
        total_commands += d.command_count
        total_queries += d.query_count
        category = d.category or category_by_slug.get(d.slug, "")
        _bump(vendors, d.manufacturer or "unknown", d)
        _bump(categories, category or "uncategorised", d)
        device_rows.append(
            {
                "slug": d.slug,
                "name": d.name,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "series": d.series,
                "category": category,
                "url": d.url,
                "scraped": True,
                "stats": d.stats(),
                "manual": d.manual,
                "date_modified": d.date_modified,
            }
        )

    # catalog-only entries (discovered, not yet scraped)
    for entry in catalog:
        if entry.slug in by_slug:
            continue
        device_rows.append(
            {
                "slug": entry.slug,
                "name": entry.name,
                "category": entry.category,
                "url": entry.url,
                "scraped": False,
            }
        )
        _bump(categories, entry.category or "uncategorised", None)

    device_rows.sort(key=lambda r: r["slug"])
    generated = generated_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_version": dataset_version or None,
        "hil_version": __version__,
        "generated_at": generated,
        "source": source or INDEX_URL,
        "totals": {
            "devices_scraped": len(devices),
            "devices_known": len({r["slug"] for r in device_rows}),
            "commands": total_commands,
            "queries": total_queries,
            "setters": total_commands - total_queries,
            "vendors": len(vendors),
            "categories": len(categories),
        },
        "vendors": _finalize_group(vendors),
        "categories": _finalize_group(categories),
        "devices": device_rows,
    }


def _bump(group: dict[str, dict[str, Any]], key: str, d: ScrapedDevice | None) -> None:
    bucket = group.setdefault(
        key, {"devices": 0, "commands": 0, "queries": 0, "slugs": []}
    )
    bucket["devices"] += 1
    if d is not None:
        bucket["commands"] += d.command_count
        bucket["queries"] += d.query_count
        bucket["slugs"].append(d.slug)


def _finalize_group(group: dict[str, dict[str, Any]]) -> "OrderedDict[str, Any]":
    out: "OrderedDict[str, Any]" = OrderedDict()
    for key in sorted(group, key=lambda k: (-group[k]["devices"], k)):
        bucket = group[key]
        bucket["slugs"] = sorted(bucket["slugs"])
        out[key] = bucket
    return out


# -- writers ------------------------------------------------------------------


def write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_device(
    out_dir: Path,
    scraped: ScrapedDevice,
    base: Device | None = None,
    fmt: str = "both",
) -> dict[str, Path]:
    """Write a device's YAML and/or JSON artefacts; returns the paths used."""
    out_dir = Path(out_dir)
    written: dict[str, Path] = {}
    if fmt in ("yaml", "both"):
        written["yaml"] = write_text(
            out_dir / f"{scraped.slug}.yaml", render_device_yaml(scraped, base)
        )
    if fmt in ("json", "both"):
        written["json"] = write_text(
            out_dir / f"{scraped.slug}.json",
            json.dumps(scraped.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )
    return written


def write_json(path: Path, payload: Any) -> Path:
    return write_text(
        Path(path), json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
