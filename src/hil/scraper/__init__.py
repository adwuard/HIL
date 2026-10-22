"""Reference-site scraper: per-instrument SCPI pages -> registry data + manifest.

Phase 2 of the HIL harness. Everything here is stdlib + PyYAML (no extra
installs). Typical use is via the CLI (``hil scrape ...``); the functions below
are the programmatic surface.

    from hil.scraper import Fetcher, run_all
    report = run_all(Path("scraped"), Fetcher(cache_dir=Path(".cache/reference")))
    print(report.manifest_path, len(report.devices), report.failed)
"""

from __future__ import annotations

from .reference import (
    BASE_URL,
    INDEX_URL,
    CatalogEntry,
    ScrapedCommand,
    ScrapedDevice,
    ScrapedParameter,
    ScrapedReturn,
    device_url,
    discover_catalog,
    parse_device,
)
from .framer import FramerHeap, extract_handover, extract_ldjson
from .http import FetchError, Fetcher, FetchResult
from .manifest import (
    build_manifest,
    command_name,
    device_to_yaml_dict,
    render_device_yaml,
    write_device,
)
from .pipeline import (
    ScrapeReport,
    fetch_catalog,
    run_all,
    run_catalog,
    run_device,
    scrape_device,
)

__all__ = [
    "BASE_URL",
    "INDEX_URL",
    "Fetcher",
    "FetchResult",
    "FetchError",
    "FramerHeap",
    "extract_handover",
    "extract_ldjson",
    "CatalogEntry",
    "ScrapedDevice",
    "ScrapedCommand",
    "ScrapedParameter",
    "ScrapedReturn",
    "device_url",
    "discover_catalog",
    "parse_device",
    "build_manifest",
    "command_name",
    "device_to_yaml_dict",
    "render_device_yaml",
    "write_device",
    "ScrapeReport",
    "fetch_catalog",
    "scrape_device",
    "run_catalog",
    "run_device",
    "run_all",
]
