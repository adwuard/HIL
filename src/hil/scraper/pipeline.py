"""Scraping pipelines: catalog, single device, and full crawl.

Glues :mod:`hil.scraper.http` (fetch), :mod:`hil.scraper.reference` (parse) and
:mod:`hil.scraper.manifest` (render) into three runnable operations used by the
``hil scrape`` CLI:

* :func:`run_catalog` -- one request -> the full device index + catalog.json
* :func:`run_device`  -- one device -> YAML + JSON
* :func:`run_all`     -- catalog + every device page -> per-device artefacts
  and a multi-level ``manifest.json``.

The HTTP cache is the resume mechanism: a re-run (or ``--offline``) serves
already-fetched pages from disk, so an interrupted 725-device crawl picks up
where it left off. Per-device failures are captured, not fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..registry import load_registry
from ..registry.schema import Device
from .reference import (
    INDEX_URL,
    CatalogEntry,
    ScrapedDevice,
    device_url,
    discover_catalog,
    parse_device,
)
from .http import Fetcher
from .manifest import build_manifest, write_device, write_json

Progress = Callable[[int, int, str, bool], None]


@dataclass
class ScrapeReport:
    out_dir: Path
    catalog: list[CatalogEntry] = field(default_factory=list)
    devices: list[ScrapedDevice] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    manifest_path: Path | None = None
    catalog_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failed


def _seeds() -> dict[str, Device]:
    """Curated registry seeds, keyed by slug (for safety/interface merge)."""
    try:
        return load_registry()
    except Exception:  # noqa: BLE001 - a broken seed must not block scraping
        return {}


def fetch_catalog(fetcher: Fetcher) -> list[CatalogEntry]:
    """Fetch the index page and return every catalogued device."""
    html = fetcher.get(INDEX_URL).text
    return discover_catalog(html)


def scrape_device(
    fetcher: Fetcher,
    slug: str,
    *,
    category: str = "",
) -> ScrapedDevice:
    """Fetch + parse one device page into a :class:`ScrapedDevice`."""
    html = fetcher.get(device_url(slug)).text
    return parse_device(html, slug, category=category)


def run_catalog(out_dir: Path, fetcher: Fetcher) -> ScrapeReport:
    """Discover the full catalog and write ``catalog.json``."""
    out_dir = Path(out_dir)
    catalog = fetch_catalog(fetcher)
    catalog_path = write_json(
        out_dir / "catalog.json",
        {
            "source": INDEX_URL,
            "count": len(catalog),
            "devices": [e.to_dict() for e in catalog],
        },
    )
    return ScrapeReport(
        out_dir=out_dir, catalog=catalog, catalog_path=catalog_path
    )


def run_device(
    out_dir: Path,
    fetcher: Fetcher,
    slug: str,
    *,
    fmt: str = "both",
    category: str = "",
) -> ScrapeReport:
    """Scrape one device and write its YAML/JSON artefacts."""
    out_dir = Path(out_dir)
    seeds = _seeds()
    scraped = scrape_device(fetcher, slug, category=category)
    write_device(out_dir / "devices", scraped, base=seeds.get(slug), fmt=fmt)
    return ScrapeReport(out_dir=out_dir, devices=[scraped])


def _select(
    catalog: list[CatalogEntry],
    *,
    limit: int | None,
    categories: tuple[str, ...],
    vendors: tuple[str, ...],
) -> list[CatalogEntry]:
    selected = catalog
    if categories:
        wanted = {c.lower() for c in categories}
        selected = [e for e in selected if e.category.lower() in wanted]
    if vendors:
        wanted_v = {v.lower() for v in vendors}
        selected = [
            e
            for e in selected
            if e.slug.split("-", 1)[0] in wanted_v
            or e.name.split(" ", 1)[0].lower() in wanted_v
        ]
    if limit is not None and limit >= 0:
        selected = selected[:limit]
    return selected


def run_all(
    out_dir: Path,
    fetcher: Fetcher,
    *,
    limit: int | None = None,
    categories: tuple[str, ...] = (),
    vendors: tuple[str, ...] = (),
    fmt: str = "both",
    force: bool = False,
    dataset_version: str = "",
    progress: Progress | None = None,
) -> ScrapeReport:
    """Full crawl: catalog + selected device pages + multi-level manifest."""
    out_dir = Path(out_dir)
    devices_dir = out_dir / "devices"
    seeds = _seeds()

    catalog = fetch_catalog(fetcher)
    catalog_path = write_json(
        out_dir / "catalog.json",
        {
            "source": INDEX_URL,
            "count": len(catalog),
            "devices": [e.to_dict() for e in catalog],
        },
    )

    selected = _select(
        catalog, limit=limit, categories=categories, vendors=vendors
    )
    total = len(selected)
    devices: list[ScrapedDevice] = []
    failed: list[tuple[str, str]] = []

    for i, entry in enumerate(selected, 1):
        slug = entry.slug
        try:
            html = fetcher.get(device_url(slug)).text
            scraped = parse_device(html, slug, category=entry.category)
            _write_unless_present(
                devices_dir, scraped, seeds.get(slug), fmt, force
            )
            devices.append(scraped)
            ok = True
        except Exception as exc:  # noqa: BLE001 - isolate per-device failures
            failed.append((slug, str(exc)))
            ok = False
        if progress is not None:
            progress(i, total, slug, ok)

    manifest = build_manifest(
        devices, catalog, source=INDEX_URL, dataset_version=dataset_version
    )
    manifest_path = write_json(out_dir / "manifest.json", manifest)

    return ScrapeReport(
        out_dir=out_dir,
        catalog=catalog,
        devices=devices,
        failed=failed,
        manifest_path=manifest_path,
        catalog_path=catalog_path,
    )


def _write_unless_present(
    devices_dir: Path,
    scraped: ScrapedDevice,
    base: Device | None,
    fmt: str,
    force: bool,
) -> None:
    primary = devices_dir / f"{scraped.slug}.json"
    if not force and fmt in ("json", "both") and primary.exists():
        return
    if not force and fmt == "yaml" and (devices_dir / f"{scraped.slug}.yaml").exists():
        return
    write_device(devices_dir, scraped, base=base, fmt=fmt)
