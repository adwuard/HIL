"""Reference-site scraper: catalog discovery + full per-device extraction.

The reference site publishes one page per instrument under
``/equipment/instruments/<slug>``. Each page embeds the *complete* SCPI
command set (hundreds of commands, with manual section/page, typed
parameters, ranges, enums, defaults, units, return specs and examples) plus
schema.org metadata. This module turns those pages into typed, lossless
Python objects (:class:`ScrapedDevice`); :mod:`hil.scraper.manifest` renders
them to registry YAML / JSON.

The concrete source host is spelled out exactly once, in :data:`BASE_URL`
below; every other module derives URLs from it and never hard-codes the host.

Two entry points:

* :func:`discover_catalog` -- the ``/equipment`` index -> every device's
  slug / name / category (one request, no per-device fetch needed).
* :func:`parse_device` -- one instrument page -> full command set + metadata.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .framer import FramerHeap, extract_handover, extract_ldjson, walk_records

# The single place the concrete source host is spelled out (see module docstring).
BASE_URL = "https://bootloop.ai"
INDEX_URL = f"{BASE_URL}/equipment"

# Framer CMS property ids for the instrument collection. Stable across the
# index and device pages today; used when present, with content-based
# fallbacks so a re-keyed CMS does not silently break the scraper.
KEY_SLUG = "hpWTu_owi"
KEY_NAME = "eRVE7hbFO"
KEY_CATEGORY = "YixQYNJC7"

_SLUG_HREF_RE = re.compile(r"instruments/([a-z0-9][a-z0-9-]*)")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COUNT_RE = re.compile(
    r"(\d+)\s+SCPI commands across\s+(\d+)\s+subsystems.*?"
    r"\((\d+)\s+queries,\s*(\d+)\s+settable",
    re.S,
)


# -- data model ---------------------------------------------------------------


@dataclass(frozen=True)
class ScrapedParameter:
    name: str = ""
    type: str = ""
    range_min: Any = None
    range_max: Any = None
    default: Any = None
    unit: str | None = None
    enum_values: tuple[str, ...] = ()
    optional: bool = False
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScrapedParameter":
        return cls(
            name=str(d.get("name", "") or ""),
            type=str(d.get("type", "") or ""),
            range_min=d.get("range_min"),
            range_max=d.get("range_max"),
            default=d.get("default"),
            unit=d.get("unit"),
            enum_values=tuple(str(x) for x in (d.get("enum_values") or ())),
            optional=bool(d.get("optional", False)),
            description=str(d.get("description", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "default": self.default,
            "unit": self.unit,
            "enum_values": list(self.enum_values),
            "optional": self.optional,
            "description": self.description,
        }


@dataclass(frozen=True)
class ScrapedReturn:
    type: str = ""
    description: str = ""
    enum_values: tuple[str, ...] = ()
    range_min: Any = None
    range_max: Any = None
    unit: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ScrapedReturn | None":
        if not d:
            return None
        return cls(
            type=str(d.get("type", "") or ""),
            description=str(d.get("description", "") or ""),
            enum_values=tuple(str(x) for x in (d.get("enum_values") or ())),
            range_min=d.get("range_min"),
            range_max=d.get("range_max"),
            unit=d.get("unit"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "description": self.description,
            "enum_values": list(self.enum_values),
            "range_min": self.range_min,
            "range_max": self.range_max,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ScrapedCommand:
    mnemonic: str
    subsystem: str = ""
    is_query: bool = False
    anchor: str = ""
    description: str = ""
    section: str = ""
    page: int | None = None
    parameters: tuple[ScrapedParameter, ...] = ()
    returns: ScrapedReturn | None = None
    examples: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScrapedCommand":
        page = d.get("page")
        return cls(
            mnemonic=str(d.get("mnemonic", "") or ""),
            subsystem=str(d.get("subsystem", "") or ""),
            is_query=bool(d.get("is_query", False)),
            anchor=str(d.get("anchor", "") or ""),
            description=str(d.get("description", "") or ""),
            section=str(d.get("section", "") or ""),
            page=int(page) if isinstance(page, (int, float)) else None,
            parameters=tuple(
                ScrapedParameter.from_dict(p)
                for p in (d.get("parameters") or ())
                if isinstance(p, dict)
            ),
            returns=ScrapedReturn.from_dict(d.get("returns")),
            examples=tuple(str(x) for x in (d.get("examples") or ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mnemonic": self.mnemonic,
            "subsystem": self.subsystem,
            "is_query": self.is_query,
            "anchor": self.anchor,
            "description": self.description,
            "section": self.section,
            "page": self.page,
            "parameters": [p.to_dict() for p in self.parameters],
            "returns": self.returns.to_dict() if self.returns else None,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the ``/equipment`` index (no per-device fetch required)."""

    slug: str
    name: str = ""
    category: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "category": self.category,
            "url": self.url or device_url(self.slug),
        }


@dataclass
class ScrapedDevice:
    """Full extracted definition for one instrument."""

    slug: str
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    series: str = ""
    category: str = ""
    description: str = ""
    manual: str = ""
    date_modified: str = ""
    keywords: tuple[str, ...] = ()
    url: str = ""
    commands: tuple[ScrapedCommand, ...] = ()
    advertised: dict[str, int] = field(default_factory=dict)

    # -- derived stats (authoritative: computed from the actual commands) ----

    @property
    def command_count(self) -> int:
        return len(self.commands)

    @property
    def query_count(self) -> int:
        return sum(1 for c in self.commands if c.is_query)

    @property
    def setter_count(self) -> int:
        return self.command_count - self.query_count

    @property
    def subsystems(self) -> tuple[str, ...]:
        return tuple(sorted({c.subsystem for c in self.commands if c.subsystem}))

    @property
    def subsystem_count(self) -> int:
        return len(self.subsystems)

    def stats(self) -> dict[str, int]:
        return {
            "commands": self.command_count,
            "queries": self.query_count,
            "setters": self.setter_count,
            "subsystems": self.subsystem_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "series": self.series,
            "category": self.category,
            "description": self.description,
            "manual": self.manual,
            "date_modified": self.date_modified,
            "keywords": list(self.keywords),
            "url": self.url or device_url(self.slug),
            "stats": self.stats(),
            "advertised": dict(self.advertised),
            "subsystems": list(self.subsystems),
            "commands": [c.to_dict() for c in self.commands],
        }


# -- URLs ---------------------------------------------------------------------


def device_url(slug: str) -> str:
    return f"{BASE_URL}/equipment/instruments/{slug}"


# -- catalog (index page) -----------------------------------------------------


def discover_catalog(index_html: str) -> list[CatalogEntry]:
    """Extract every device (slug/name/category) from the ``/equipment`` page.

    Slugs come from the rendered ``instruments/<slug>`` links (bulletproof);
    names/categories are enriched from the Framer handover records when they
    resolve. Never raises on handover changes -- degrades to slug-only.
    """
    slugs = _unique(_SLUG_HREF_RE.findall(index_html))

    records: dict[str, dict[str, str]] = {}
    try:
        heap = FramerHeap(extract_handover(index_html))
        slug_set = set(slugs)
        resolved = heap.resolve(0)
        for rec in walk_records(resolved, _is_catalog_record):
            slug, name, category = _classify_record(rec, slug_set)
            if slug and slug not in records:
                records[slug] = {"name": name, "category": category}
    except Exception:  # noqa: BLE001 - catalog enrichment is best-effort
        records = {}

    entries: list[CatalogEntry] = []
    for slug in slugs:
        meta = records.get(slug, {})
        entries.append(
            CatalogEntry(
                slug=slug,
                name=meta.get("name", ""),
                category=meta.get("category", ""),
                url=device_url(slug),
            )
        )
    return entries


def _is_catalog_record(obj: Any) -> bool:
    """A resolved index record: small all-string dict holding a slug."""
    if not isinstance(obj, dict) or not (2 <= len(obj) <= 8):
        return False
    values = list(obj.values())
    if not all(isinstance(v, str) for v in values):
        return False
    return any(_SLUG_RE.match(v) for v in values)


def _classify_record(
    rec: dict[str, str], slug_set: set[str]
) -> tuple[str, str, str]:
    """Pull (slug, name, category) from a record, keys-first then by content."""
    values = list(rec.values())

    slug = rec.get(KEY_SLUG, "")
    if not slug or slug not in slug_set:
        slug = next((v for v in values if v in slug_set), "")
    if not slug:
        slug = next((v for v in values if _SLUG_RE.match(v)), "")

    category = rec.get(KEY_CATEGORY, "")
    if not category:
        category = next(
            (
                v
                for v in values
                if v and v != slug and v == v.lower() and " " in v
            ),
            "",
        )

    name = rec.get(KEY_NAME, "")
    if not name:
        candidates = [v for v in values if v not in (slug, category) and v]
        # name is the spaced / title-ish candidate; the record id is a single
        # dense token, so prefer one with a space, else the longest.
        name = next((v for v in candidates if " " in v), "") or (
            max(candidates, key=len) if candidates else ""
        )
    return slug, name, category


# -- device page --------------------------------------------------------------


def parse_device(
    device_html: str,
    slug: str,
    *,
    url: str = "",
    category: str = "",
) -> ScrapedDevice:
    """Parse one instrument page into a :class:`ScrapedDevice`.

    Command data is harvested from the flat handover scalars (the command set
    is stored as one big JSON string element), and semantic metadata from the
    ``ld+json`` block. Counts are *derived* from the parsed commands so they
    are authoritative regardless of what the page advertises.
    """
    heap = FramerHeap(extract_handover(device_html))
    strings = heap.strings()

    commands = _parse_commands(strings)
    ld = extract_ldjson(device_html) or {}
    article, dataset, breadcrumb = _ld_nodes(ld)

    name = _name_from(article, dataset, strings, slug)
    manufacturer, series = _vendor_series(breadcrumb)
    if not manufacturer:
        manufacturer = _guess_manufacturer(strings, name)
    model = _derive_model(name, manufacturer)

    description = _longest_prose(strings) or str(dataset.get("description", ""))
    manual = str(article.get("isBasedOn", "") or dataset.get("isBasedOn", "") or "")
    date_modified = _find_date(strings) or str(article.get("dateModified", "") or "")
    keywords = tuple(article.get("keywords", ()) or ())
    advertised = _advertised_counts(dataset)

    if not category:
        category = _category_from(article)

    return ScrapedDevice(
        slug=slug,
        name=name,
        manufacturer=manufacturer,
        model=model,
        series=series,
        category=category,
        description=description,
        manual=manual,
        date_modified=date_modified,
        keywords=keywords,
        url=url or device_url(slug),
        commands=tuple(commands),
        advertised=advertised,
    )


def _parse_commands(strings: Iterable[str]) -> list[ScrapedCommand]:
    """Find the command-set JSON string and decode it into commands."""
    for s in strings:
        head = s.lstrip()
        if not head.startswith("["):
            continue
        if '"mnemonic"' not in s[:400]:
            continue
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return [
                ScrapedCommand.from_dict(c)
                for c in parsed
                if isinstance(c, dict)
            ]
    return []


def _ld_nodes(ld: dict[str, Any]) -> tuple[dict, dict, list]:
    graph = ld.get("@graph") if isinstance(ld, dict) else None
    nodes = graph if isinstance(graph, list) else ([ld] if ld else [])
    article = dataset = {}
    breadcrumb: list = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = node.get("@type")
        if ntype == "TechArticle":
            article = node
        elif ntype == "Dataset":
            dataset = node
        elif ntype == "BreadcrumbList":
            breadcrumb = node.get("itemListElement", []) or []
    return article, dataset, breadcrumb


def _vendor_series(breadcrumb: list) -> tuple[str, str]:
    """Vendor/series names, detected by their ``/vendors/`` ``/series/`` URLs."""
    vendor = series = ""
    for item in breadcrumb:
        if not isinstance(item, dict):
            continue
        target = str(item.get("item", ""))
        label = str(item.get("name", ""))
        if "/vendors/" in target and not vendor:
            vendor = label
        elif "/series/" in target and not series:
            series = label
    return vendor, series


def _name_from(article: dict, dataset: dict, strings: list[str], slug: str) -> str:
    headline = str(article.get("headline", "") or "")
    if headline:
        return re.sub(r"\s+SCPI Command Reference$", "", headline).strip()
    ds_name = str(dataset.get("name", "") or "")
    if ds_name:
        return re.sub(r"\s+SCPI command set$", "", ds_name).strip()
    # fall back to the first title-ish scalar in the heap
    for s in strings:
        if s and s[0].isupper() and len(s) < 60 and " " in s:
            return s
    return slug


def _guess_manufacturer(strings: list[str], name: str) -> str:
    """Last short capitalised scalar that is not the name/description."""
    best = ""
    for s in strings:
        if not s or len(s) > 40 or s == name or " " in s.strip():
            continue
        if s[:1].isupper() and not _DATE_RE.match(s):
            best = s
    if best:
        return best
    return name.split(" ", 1)[0] if name else ""


def _derive_model(name: str, manufacturer: str) -> str:
    if name and manufacturer and name.lower().startswith(manufacturer.lower()):
        return name[len(manufacturer) :].strip()
    return name


def _longest_prose(strings: Iterable[str]) -> str:
    best = ""
    for s in strings:
        stripped = s.lstrip()
        if len(s) < 80 or stripped[:1] in "[{":
            continue
        if len(s) > len(best):
            best = s
    return best


def _find_date(strings: Iterable[str]) -> str:
    for s in strings:
        if _DATE_RE.match(s):
            return s
    return ""


def _advertised_counts(dataset: dict) -> dict[str, int]:
    text = str(dataset.get("description", "") or "")
    m = _COUNT_RE.search(text)
    if not m:
        return {}
    commands, subsystems, queries, setters = (int(x) for x in m.groups())
    return {
        "commands": commands,
        "subsystems": subsystems,
        "queries": queries,
        "setters": setters,
    }


def _category_from(article: dict) -> str:
    # articleSection is "Test & Measurement Automation" site-wide, not a
    # per-device category; keep it only as a weak hint.
    return ""


def _unique(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
