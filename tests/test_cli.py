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


def test_exec_works_with_non_scpi_driver(monkeypatch, capsys):
    """Regression: `hil exec` must work with *any* BaseInstrument driver, not
    just SCPI. safe_execute used to return a bare string for non-SCPI drivers,
    so cmd_exec crashed with `TypeError: string indices must be integers`."""
    from hil.errors import HilError
    from hil.instrument import BaseInstrument

    class _Drv(BaseInstrument):
        def idn(self) -> str:
            return "DRV,MODEL,SN,FW"

        def query(self, command, timeout=None):
            if command.startswith("BOOM"):
                raise HilError("kaboom")
            return "7"

        def write(self, command) -> None:
            pass

        def drain_errors(self) -> list[str]:
            return []  # non-SCPI driver: no SYST:ERR? queue to poll

        def close(self) -> None:
            pass

    monkeypatch.setattr("hil.cli.connect", lambda *a, **k: _Drv())
    code = main(["exec", "drv://x", ":MEAS:CURR?", "BOOM?", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1  # the BOOM? command failed
    results = payload["results"]
    assert results[0]["response"] == "7"
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert "kaboom" in results[1]["error"]


def test_log_setup_runs_before_capture(monkeypatch, capsys, tmp_path):
    """`hil log --setup CMD` sends CMD (via safe_execute) before capturing, so a
    PPK2 can set ':CONF:MODE AMP' / ':SOUR:VOLT' before sampling."""
    from hil.instrument import BaseInstrument

    calls: list[tuple] = []

    class _Drv(BaseInstrument):
        def idn(self) -> str:
            return "DRV,MODEL,SN,FW"

        def query(self, command, timeout=None):
            return "0"

        def write(self, command) -> None:
            calls.append(("write", command))

        def drain_errors(self) -> list[str]:
            return []

        def capture(self, channel, seconds, interval=None, units=""):
            calls.append(("capture", channel))
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr("hil.cli.connect", lambda *a, **k: _Drv())
    out = tmp_path / "cap.csv"
    code = main([
        "log", "drv://x", "--channel", "current", "--seconds", "1",
        "--setup", ":CONF:MODE AMP", "--setup", ":SOUR:VOLT 3300",
        "--out", str(out), "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert calls[0] == ("write", ":CONF:MODE AMP")
    assert calls[1] == ("write", ":SOUR:VOLT 3300")
    assert calls[-1] == ("capture", "current")


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


# -- hil debug (flash + on-chip debug on the in-memory simulator) -----------
def test_debug_probe_sim(capsys):
    code = main(["debug", "probe", "--sim", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert len(payload["probes"]) == 1
    assert payload["probes"][0]["transport"] == "swd"


def test_debug_info_sim(capsys):
    code = main(["debug", "info", "--sim", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["chip"] == "sim-target" and payload["core"] == "cortex-m4"
    assert payload["state"] == "running"


def test_debug_flash_sim(tmp_path, capsys):
    fw = tmp_path / "app.bin"
    fw.write_bytes(b"\x00\x01\x02\x03firmware")
    code = main(["debug", "flash", "--sim", str(fw), "--chip", "nrf52840", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["kind"] == "flash" and payload["verified"] is True
    assert payload["size_bytes"] == fw.stat().st_size and len(payload["sha256"]) == 64


def test_debug_read_mem_and_reg_sim(capsys):
    code = main(["debug", "read", "--sim", "--mem", "0x40000000", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["values"] == [0x71] and payload["hex"] == ["0x71"]

    code = main(["debug", "read", "--sim", "--reg", "pc", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["register"] == "pc" and payload["hex"] == "0x8000150"


def test_debug_write_then_read_mem_sim(capsys):
    code = main(["debug", "write", "--sim", "--mem", "0x20000000", "--value", "0xDEADBEEF", "--json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"] is True


def test_debug_run_control_sim(capsys):
    code = main(["debug", "halt", "--sim", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["state"] == "halted"
    code = main(["debug", "resume", "--sim", "--json"])
    assert json.loads(capsys.readouterr().out)["state"] == "running"


def test_debug_rtt_writes_out_file(tmp_path, capsys):
    out = tmp_path / "rtt.txt"
    code = main(["debug", "rtt", "--sim", "--seconds", "0.02", "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["line_count"] == 4 and payload["path"] == str(out)
    assert "wifi_connected" in out.read_text(encoding="utf-8")


def test_debug_gdb_sim(tmp_path, capsys):
    script = tmp_path / "dump.gdb"
    script.write_text("monitor reset halt\n", encoding="utf-8")
    out = tmp_path / "gdb.log"
    code = main(["debug", "gdb", "--sim", str(script), "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True and payload["exit_code"] == 0


def test_debug_read_without_selector_errors(capsys):
    code = main(["debug", "read", "--sim", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["ok"] is False
    assert "--mem" in payload["error"] or "--reg" in payload["error"]


def test_debug_requires_a_target(capsys):
    code = main(["debug", "probe", "--json"])          # no address, no --sim
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["ok"] is False
    assert "debug://" in payload["error"]


def test_debug_human_output(capsys):
    code = main(["debug", "probe", "--sim"])
    out = capsys.readouterr().out
    assert code == 0 and "1 probe(s)" in out and "Simulated CMSIS-DAP" in out
