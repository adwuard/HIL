"""Scraper tests — fully offline, driven by tests/fixtures/*.html.

The fixtures mirror the reference site's real Framer payload (a flat handover heap +
an ld+json block); regenerate with ``python tests/fixtures/_generate.py``.
"""

from __future__ import annotations

import json

import pytest

from hil.registry import load_device_file
from hil.registry.schema import Device, SafetyPolicy
from hil.scraper.reference import (
    INDEX_URL,
    ScrapedCommand,
    device_url,
    discover_catalog,
    parse_device,
)
from hil.scraper.framer import (
    FramerError,
    FramerHeap,
    extract_handover,
    extract_ldjson,
)
from hil.scraper.http import FetchError, Fetcher, FetchResult
from hil.scraper.manifest import (
    build_manifest,
    command_name,
    render_device_yaml,
    summarize_params,
    write_device,
)
from hil.scraper.pipeline import run_all, run_catalog, run_device


@pytest.fixture(scope="module")
def device(device_html) -> "object":
    return parse_device(device_html, "fake-scope-1000")


# -- framer -------------------------------------------------------------------


def test_extract_handover_and_ldjson(device_html):
    heap = extract_handover(device_html)
    assert isinstance(heap, list) and heap
    ld = extract_ldjson(device_html)
    assert ld and ld["@context"] == "https://schema.org"
    types = {n["@type"] for n in ld["@graph"]}
    assert {"TechArticle", "Dataset", "BreadcrumbList"} <= types


def test_extract_handover_missing_raises():
    with pytest.raises(FramerError):
        extract_handover("<html><body>no framer here</body></html>")


def test_heap_resolves_maps_typed_values_and_refs():
    # heap: 0 root obj -> {a: Map, b: typed}; Map{k:v}; typed 'string'->'X'
    data = [
        {"a": 1, "b": 4},        # 0 root
        ["Map", 2, 3],           # 1 Map
        "key",                   # 2
        "value",                 # 3
        {"type": 5, "value": 6}, # 4 typed wrapper
        "string",                # 5
        "X",                     # 6
    ]
    resolved = FramerHeap(data).resolve(0)
    assert resolved == {"a": {"key": "value"}, "b": "X"}


def test_heap_scalars_and_strings(device_html):
    heap = FramerHeap(extract_handover(device_html))
    assert "Fake Scope 1000" in heap.strings()
    assert any(isinstance(x, int) for x in heap.scalars())


# -- catalog ------------------------------------------------------------------


def test_discover_catalog(index_html):
    catalog = discover_catalog(index_html)
    by_slug = {e.slug: e for e in catalog}
    assert set(by_slug) == {"fake-scope-1000", "fake-dmm-2000", "fake-psu-3000"}
    assert by_slug["fake-scope-1000"].name == "Fake Scope 1000"
    assert by_slug["fake-scope-1000"].category == "oscilloscope"
    assert by_slug["fake-psu-3000"].category == "power supply"
    assert by_slug["fake-dmm-2000"].url == device_url("fake-dmm-2000")


def test_discover_catalog_degrades_without_handover():
    # slugs still recovered from rendered links even if the heap is absent
    html = '<a href="./equipment/instruments/only-slug">x</a>'
    catalog = discover_catalog(html)
    assert [e.slug for e in catalog] == ["only-slug"]
    assert catalog[0].name == "" and catalog[0].category == ""


# -- device -------------------------------------------------------------------


def test_parse_device_metadata(device):
    assert device.name == "Fake Scope 1000"
    assert device.manufacturer == "Fake"
    assert device.model == "Scope 1000"
    assert device.series == "FS1000 Series"
    assert device.manual == "FS1000 Programming Guide"
    assert device.date_modified == "2026-01-02"
    assert device.description.startswith("The Fake Scope 1000 is")
    assert "Fake Scope 1000 SCPI" in device.keywords


def test_parse_device_commands_and_counts(device):
    assert device.command_count == 3
    assert device.query_count == 1
    assert device.setter_count == 2
    assert device.subsystem_count == 3
    # derived counts are authoritative and agree with the advertised ones
    adv = device.advertised
    assert adv["commands"] == device.command_count
    assert adv["queries"] == device.query_count
    assert adv["subsystems"] == device.subsystem_count


def test_parse_device_command_detail(device):
    by_mnemonic = {c.mnemonic: c for c in device.commands}
    vpp = by_mnemonic[":MEASure:VPP"]
    assert vpp.is_query and vpp.anchor == "measure-vpp"
    assert vpp.section == "3.4" and vpp.page == 22
    p = vpp.parameters[0]
    assert p.name == "source" and p.type == "enum"
    assert p.enum_values == ("CHAN1", "CHAN2") and p.optional and p.default == "CHAN1"
    assert vpp.returns is not None and vpp.returns.unit == "V"
    assert vpp.examples and "MEASure:VPP?" in vpp.examples[0]
    scale = by_mnemonic[":CHANnel<n>:SCALe"]
    assert scale.parameters[0].range_min == 0.001
    assert scale.parameters[0].range_max == 1000.0


# -- manifest / rendering -----------------------------------------------------


def test_command_name_prefers_anchor_then_mnemonic():
    assert command_name(_cmd(anchor="meas-vpp", mnemonic=":X")) == "meas_vpp"
    assert command_name(_cmd(anchor="", mnemonic=":MEAS:VPP?")) == "meas_vpp_query"


def test_summarize_params(device):
    by_mnemonic = {c.mnemonic: c for c in device.commands}
    assert summarize_params(by_mnemonic[":MEASure:VPP"]) == "<source?: CHAN1|CHAN2>"
    assert summarize_params(by_mnemonic[":CHANnel<n>:SCALe"]) == "<scale: 0.001..1000.0 V>"
    assert summarize_params(by_mnemonic[":CLEar"]) == ""


def test_render_yaml_roundtrips_through_loader(device, tmp_path):
    path = tmp_path / "fake-scope-1000.yaml"
    path.write_text(render_device_yaml(device), encoding="utf-8")
    loaded = load_device_file(path)
    assert loaded.slug == "fake-scope-1000"
    assert loaded.manufacturer == "Fake" and loaded.model == "Scope 1000"
    assert loaded.series == "FS1000 Series"
    assert loaded.manual == "FS1000 Programming Guide"
    assert len(loaded.commands) == 3
    vpp = next(c for c in loaded.commands if c.syntax == ":MEASure:VPP")
    assert vpp.query and vpp.section == "3.4" and vpp.page == 22
    assert vpp.param_specs[0]["enum_values"] == ["CHAN1", "CHAN2"]
    assert vpp.returns["unit"] == "V" and vpp.examples


def test_render_yaml_merges_curated_seed(device):
    base = Device(
        slug="fake-scope-1000",
        manufacturer="Fake",
        model="Scope 1000",
        category="oscilloscope",
        interfaces=("lan", "usb"),
        connection_examples=("tcp://10.0.0.9:5555",),
        capabilities=("acquire-waveform",),
        safety=SafetyPolicy(blocked=("CAL.*",), limits={"voltage": "0..100 V"}),
    )
    text = render_device_yaml(device, base=base)
    assert "blocked:" in text and "CAL.*" in text
    assert "tcp://10.0.0.9:5555" in text
    assert "acquire-waveform" in text
    # scraped commands still fully present
    assert ":MEASure:VPP" in text and ":CHANnel<n>:SCALe" in text


def test_build_manifest_levels(device, index_html):
    catalog = discover_catalog(index_html)
    man = build_manifest([device], catalog)
    assert man["totals"]["devices_scraped"] == 1
    assert man["totals"]["devices_known"] == 3
    assert man["totals"]["commands"] == 3
    assert man["vendors"]["Fake"]["devices"] == 1
    # categories come from the full catalog (scraped + discovered)
    assert "oscilloscope" in man["categories"]
    scraped = [r for r in man["devices"] if r["scraped"]]
    assert len(scraped) == 1 and scraped[0]["slug"] == "fake-scope-1000"
    assert scraped[0]["stats"]["commands"] == 3


def test_write_device_both_formats(device, tmp_path):
    written = write_device(tmp_path, device, fmt="both")
    assert written["yaml"].exists() and written["json"].exists()
    data = json.loads(written["json"].read_text())
    assert data["slug"] == "fake-scope-1000"
    assert len(data["commands"]) == 3
    assert data["stats"]["queries"] == 1


# -- http fetcher -------------------------------------------------------------


def test_fetcher_caches_and_offline(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_download(self, url):
        calls["n"] += 1
        return "<html>hi</html>", 200

    monkeypatch.setattr(Fetcher, "_download", fake_download)
    f = Fetcher(cache_dir=tmp_path, min_delay=0)
    r1 = f.get("http://example.test/a")
    assert r1.text == "<html>hi</html>" and not r1.from_cache
    r2 = f.get("http://example.test/a")
    assert r2.from_cache and calls["n"] == 1  # second served from disk

    offline = Fetcher(cache_dir=tmp_path, offline=True, min_delay=0)
    assert offline.get("http://example.test/a").from_cache
    with pytest.raises(FetchError):
        offline.get("http://example.test/MISSING")


# -- pipeline (offline via a fake fetcher) ------------------------------------


class FakeFetcher(Fetcher):
    """Fetcher that serves a fixed {url: html} map, no network/cache."""

    def __init__(self, pages: dict[str, str]) -> None:
        super().__init__(cache_dir=None, min_delay=0)
        self._pages = pages

    def get(self, url: str) -> FetchResult:  # type: ignore[override]
        if url not in self._pages:
            raise FetchError(f"not found: {url}")
        return FetchResult(url, self._pages[url], 200, from_cache=True)


@pytest.fixture
def fake_fetcher(index_html, device_html) -> FakeFetcher:
    return FakeFetcher(
        {
            INDEX_URL: index_html,
            device_url("fake-scope-1000"): device_html,
        }
    )


def test_run_catalog(tmp_path, fake_fetcher):
    report = run_catalog(tmp_path, fake_fetcher)
    assert len(report.catalog) == 3
    assert report.catalog_path.exists()
    payload = json.loads(report.catalog_path.read_text())
    assert payload["count"] == 3


def test_run_device(tmp_path, fake_fetcher):
    report = run_device(tmp_path, fake_fetcher, "fake-scope-1000", fmt="both")
    dev = report.devices[0]
    assert dev.command_count == 3
    assert (tmp_path / "devices" / "fake-scope-1000.yaml").exists()
    assert (tmp_path / "devices" / "fake-scope-1000.json").exists()


def test_run_all_filtered_and_manifest(tmp_path, fake_fetcher):
    report = run_all(
        tmp_path,
        fake_fetcher,
        categories=("oscilloscope",),  # only fake-scope-1000 is fetchable
        fmt="json",
    )
    assert report.ok and len(report.devices) == 1
    assert report.manifest_path.exists() and report.catalog_path.exists()
    man = json.loads(report.manifest_path.read_text())
    assert man["totals"]["devices_scraped"] == 1
    assert man["totals"]["devices_known"] == 3


def test_run_all_isolates_failures(tmp_path, fake_fetcher):
    # no category filter -> tries all 3, but only fake-scope-1000 is served
    report = run_all(tmp_path, fake_fetcher, fmt="json")
    assert len(report.devices) == 1
    failed_slugs = {s for s, _ in report.failed}
    assert {"fake-dmm-2000", "fake-psu-3000"} <= failed_slugs
    assert report.manifest_path.exists()  # manifest still written


def _cmd(anchor: str, mnemonic: str) -> ScrapedCommand:
    """Tiny helper mirroring a scraped command for command_name()."""
    return ScrapedCommand(mnemonic=mnemonic, anchor=anchor)
