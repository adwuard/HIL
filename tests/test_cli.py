from __future__ import annotations

import json

from hil.cli import main
from hil.scraper.reference import INDEX_URL, device_url
from hil.scraper.http import Fetcher


def _seed_cache(cache_dir, pages: dict[str, str]) -> None:
    """Pre-populate a Fetcher cache so `hil scrape --offline` needs no network."""
    f = Fetcher(cache_dir=cache_dir, min_delay=0)
    for url, html in pages.items():
        f._cache_write(url, html)


def test_doctor(capsys):
    code = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["backends"]["tcp"] is True
    assert payload["registry_devices"] >= 4


def test_devices_list_and_show(capsys):
    code = main(["devices", "list", "--json"])
    assert code == 0
    listing = json.loads(capsys.readouterr().out)
    slugs = {d["slug"] for d in listing["devices"]}
    assert "keithley-2400" in slugs

    code = main(["devices", "show", "keithley-2400", "--json"])
    assert code == 0
    device = json.loads(capsys.readouterr().out)
    assert device["manufacturer"] == "Keithley"
    assert any(c["syntax"] == ":SOUR:VOLT <v>" for c in device["commands"])


def test_probe(fake_address, capsys):
    code = main(["probe", fake_address, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["identity"]["model"] == "TEST-1000"


def test_exec_queries_and_settings(fake_address, capsys):
    code = main(["exec", fake_address, "*IDN?", "OUTP ON", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    results = payload["results"]
    assert results[0]["response"] == "FAKE,TEST-1000,SN123456,V1.0"
    assert results[1]["response"] is None  # setting, not query


def test_exec_safety_block(fake_address, capsys):
    code = main(
        ["exec", fake_address, "OUTP ON", "--device", "keysight-ac6801b", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "blocked" in payload["error"] or "not on the device allow-list" in payload["error"]


def test_exec_unknown_slug_error(capsys):
    code = main(["exec", "no-such-device", "*IDN?", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False


def test_errors_queue_empty(fake_address, capsys):
    code = main(["errors", fake_address, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["errors"] == []


def test_slug_without_address_gives_examples(capsys):
    code = main(["idn", "keithley-2400", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "no default address" in payload["error"]
    assert "visa://GPIB::24::INSTR" in payload["error"]


def test_scrape_catalog_offline(tmp_path, capsys, index_html):
    cache, out = tmp_path / "cache", tmp_path / "out"
    _seed_cache(cache, {INDEX_URL: index_html})
    code = main(
        [
            "scrape", "catalog",
            "--offline", "--cache-dir", str(cache), "--out", str(out), "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True and payload["count"] == 3
    assert payload["requests"] == 0  # served entirely from cache
    assert (out / "catalog.json").exists()


def test_scrape_device_offline(tmp_path, capsys, index_html, device_html):
    cache, out = tmp_path / "cache", tmp_path / "out"
    _seed_cache(
        cache,
        {INDEX_URL: index_html, device_url("fake-scope-1000"): device_html},
    )
    code = main(
        [
            "scrape", "device", "fake-scope-1000",
            "--offline", "--cache-dir", str(cache), "--out", str(out), "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["slug"] == "fake-scope-1000"
    assert payload["stats"]["commands"] == 3
    assert (out / "devices" / "fake-scope-1000.yaml").exists()
    assert (out / "devices" / "fake-scope-1000.json").exists()


def test_scrape_all_offline_manifest(tmp_path, capsys, index_html, device_html):
    cache, out = tmp_path / "cache", tmp_path / "out"
    _seed_cache(
        cache,
        {INDEX_URL: index_html, device_url("fake-scope-1000"): device_html},
    )
    code = main(
        [
            "scrape", "all",
            "--offline", "--cache-dir", str(cache), "--out", str(out),
            "--category", "oscilloscope", "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["devices_scraped"] == 1
    assert payload["catalog_count"] == 3
    assert (out / "manifest.json").exists()
