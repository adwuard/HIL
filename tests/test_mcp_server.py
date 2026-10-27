"""Tests for the HIL MCP server (hil.mcp_server).

Two layers, both hardware-free:
* pure logic — the tool functions and the global-registration helpers, which need
  no MCP SDK (importing hil.mcp_server only *optionally* pulls in `mcp`);
* registration — building the server and enumerating its tools/resources, which
  needs the SDK and is skipped when it isn't installed.

The registration/install tests run against a throwaway ``HIL_MCP_HOME`` so they
never touch the developer's real ~/.claude.json, ~/.codex, ~/.cursor, etc.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from hil import mcp_server as M


# --- pure logic: launch command + config snippets --------------------------
def test_server_command_uses_hil_on_path(monkeypatch):
    # the idiomatic local stdio command: an absolute `hil` + the `mcp` subcommand
    monkeypatch.setattr(M.shutil, "which", lambda name: "/fake/bin/hil" if name == "hil" else None)
    assert M.server_command() == ["/fake/bin/hil", "mcp"]


def test_server_command_falls_back_to_adjacent_console_script(monkeypatch):
    # not on PATH, but a `hil` sits beside the running interpreter -> use it
    monkeypatch.setattr(M.shutil, "which", lambda name: None)
    cmd = M.server_command()
    assert cmd[1:] == ["mcp"]
    assert os.path.isabs(cmd[0]) and os.path.basename(cmd[0]).lower().startswith("hil")


def test_server_command_module_fallback(monkeypatch):
    # no console script anywhere -> <python> -m hil.mcp_server
    monkeypatch.setattr(M.shutil, "which", lambda name: None)
    monkeypatch.setattr(M.Path, "exists", lambda self: False)
    assert M.server_command() == [M.sys.executable, "-m", "hil.mcp_server"]


def test_config_snippet_json_agents_use_mcp_servers_key():
    snip = M.config_snippet(agent="cursor", include_path=False)
    assert "mcpServers" in snip and '"hil"' in snip
    assert M.server_command()[0] in snip  # spawns the local `hil mcp` command


def test_config_snippet_codex_uses_toml_block():
    snip = M.config_snippet(agent="codex", include_path=False)
    assert "[mcp_servers.hil]" in snip
    assert "command =" in snip and "args =" in snip


def test_server_entry_captures_path_when_asked(monkeypatch):
    monkeypatch.setenv("PATH", "/fake/bin:/usr/bin")
    assert M.server_entry(include_path=True)["env"]["PATH"] == "/fake/bin:/usr/bin"
    assert "env" not in M.server_entry(include_path=False)


# --- pure logic: global registration (sandboxed home) ----------------------
def test_install_merges_json_preserving_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    cfg = cursor_dir / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"keepme": {"command": "x"}}}))

    report = M.install(agent="cursor", include_path=False)
    assert report["ok"] and report["targets"][0]["changed"] == "added"

    data = json.loads(cfg.read_text())
    assert "keepme" in data["mcpServers"]          # pre-existing server preserved
    assert data["mcpServers"]["hil"]["args"] == M.server_command()[1:]


def test_install_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    (tmp_path / ".cursor").mkdir()
    first = M.install(agent="cursor", include_path=False)
    second = M.install(agent="cursor", include_path=False)
    assert first["targets"][0]["changed"] == "added"
    assert second["targets"][0]["changed"] == "updated"   # not duplicated


def test_install_codex_toml_append_then_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    cfg = codex_dir / "config.toml"
    cfg.write_text('model = "o3"\n\n[mcp_servers.other]\ncommand = "y"\n')

    M.install(agent="codex", include_path=False)
    text = cfg.read_text()
    assert 'model = "o3"' in text                    # existing settings preserved
    assert "[mcp_servers.other]" in text             # other MCP servers preserved
    assert text.count("[mcp_servers.hil]") == 1

    M.install(agent="codex", include_path=False)     # re-run must not duplicate
    assert cfg.read_text().count("[mcp_servers.hil]") == 1


def test_install_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    (tmp_path / ".cursor").mkdir()
    report = M.install(agent="cursor", dry_run=True, include_path=False)
    assert report["dry_run"] and report["targets"][0]["changed"] == "added"
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


def test_install_rejects_bad_json_without_clobbering(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    (tmp_path / ".cursor").mkdir()
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.write_text("{ not valid json")
    report = M.install(agent="cursor", include_path=False)
    assert report["targets"][0]["ok"] is False
    assert cfg.read_text() == "{ not valid json"     # left untouched


def test_install_unknown_agent_raises():
    from hil.errors import HilError

    with pytest.raises(HilError):
        M.install(agent="not-a-real-agent")


def test_detect_agents_respects_sandbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    assert M.detect_agents() == []
    (tmp_path / ".codex").mkdir()
    assert M.detect_agents() == ["codex"]


def test_install_qoder_prefers_qoder_cn_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    (tmp_path / ".qoder-cn").mkdir()      # QoderCN flavour
    (tmp_path / ".qoder").mkdir()         # international flavour also present
    cfg = tmp_path / ".qoder-cn" / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"keepme": {"command": "x"}}}))

    assert "qoder" in M.detect_agents()
    report = M.install(agent="qoder", include_path=False)
    assert report["targets"][0]["path"] == str(cfg)     # CN home wins
    data = json.loads(cfg.read_text())
    assert "keepme" in data["mcpServers"]               # pre-existing server preserved
    assert data["mcpServers"]["hil"]["args"] == M.server_command()[1:]
    assert not (tmp_path / ".qoder" / "mcp.json").exists()   # intl left untouched


def test_install_qoder_falls_back_to_international_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_MCP_HOME", str(tmp_path))
    (tmp_path / ".qoder").mkdir()         # only the international flavour exists
    report = M.install(agent="qoder", include_path=False)
    cfg = tmp_path / ".qoder" / "mcp.json"
    assert report["targets"][0]["path"] == str(cfg)
    assert json.loads(cfg.read_text())["mcpServers"]["hil"]["args"] == M.server_command()[1:]


# --- pure logic: tools that need no hardware -------------------------------
def test_hil_devices_lists_and_limits():
    res = M.hil_devices(limit=3)
    assert res["ok"] is True
    assert 1 <= res["count"] <= 3
    assert all({"slug", "manufacturer", "driver"} <= set(d) for d in res["devices"])


def test_hil_devices_search_filters():
    res = M.hil_devices(search="esp32")
    assert res["ok"] is True
    assert res["count"] >= 1
    assert any("esp32" in d["slug"] for d in res["devices"])


def test_hil_device_unknown_slug_returns_structured_error():
    res = M.hil_device("definitely-not-a-device")
    assert res["ok"] is False and "error" in res


def test_hil_debug_simulator_read_is_hardware_free():
    res = M.hil_debug("debug://simulator", "read", address="0x40000000")
    assert res["ok"] is True
    assert res["values"] == [0x71]           # WHO_AM_I on the in-memory target
    assert res["hex"] == ["0x71"]


def test_hil_debug_simulator_info():
    # The simulator reports a target state with no hardware attached.
    info = M.hil_debug("debug://simulator", "info")
    assert info["ok"] is True


def test_hil_debug_requires_value_for_write():
    res = M.hil_debug("debug://simulator", "write", address="0x20000000")
    assert res["ok"] is False and "values" in res["error"]


def test_hil_debug_unknown_action_is_structured_error():
    res = M.hil_debug("debug://simulator", "bogus")
    assert res["ok"] is False and "unknown action" in res["error"]


def test_safe_wrapper_never_raises():
    # A tool that blows up still returns {ok: False, error: ...}, never an exception.
    res = M.hil_probe("tcp://127.0.0.1:1")   # nothing listening -> error, not raise
    assert isinstance(res, dict) and res.get("ok") is False


# --- registration: needs the mcp SDK ---------------------------------------
def test_build_server_registers_expected_tools_and_resources():
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    srv = M.build_server()
    tools = asyncio.run(srv.list_tools())
    names = {t.name for t in tools}
    expected = {
        "hil_doctor", "hil_devices", "hil_device", "hil_probe", "hil_exec",
        "hil_errors", "hil_capture", "hil_debug_probe", "hil_debug", "hil_setup",
    }
    assert expected <= names
    # every tool carries a description and a JSON input schema
    for t in tools:
        assert t.description
        assert isinstance(t.input_schema, dict)

    templates = asyncio.run(srv.list_resource_templates())
    uris = {getattr(r, "uri_template", getattr(r, "uriTemplate", None)) for r in templates}
    assert "hil://devices/{slug}" in uris
    statics = asyncio.run(srv.list_resources())
    assert any(str(getattr(r, "uri", r)) == "hil://devices" for r in statics)


def test_hil_debug_tool_schema_exposes_action_and_target():
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    srv = M.build_server()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    props = (tools["hil_debug"].input_schema or {}).get("properties", {})
    assert "action" in props and "target" in props and "firmware" in props
