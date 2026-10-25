"""Tests for the on-demand provisioning subsystem (hil.provision).

Covers the four layers: the declarative **recipe** model + validation, host
**platform** detection, the **engine** (command builders, gating, execution,
probes), and the **catalog**/public API/CLI wiring. Native/network work is kept
out: builds are exercised via harmless ``shell`` steps and dry-runs, and the
``daemon`` probe is tested against a local fake socket.
"""

from __future__ import annotations

import json
import socket
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import hil
from hil import provision
from hil.cli import main
from hil.errors import HilError, ProvisionError, TransportNotFoundError
from hil.provision import catalog, engine, platform_info
from hil.provision.engine import Provisioner, _split_hostport
from hil.provision.platform_info import LINUX, MACOS, WINDOWS, Platform, detect
from hil.provision.recipe import Probe, Recipe, Step, load_recipe_file, parse_recipe


# -- helpers ---------------------------------------------------------------
def _prov(tmp_path: Path, os_name: str = MACOS, *, tools=None, pkg=None) -> Provisioner:
    plat = Platform(os=os_name, arch="arm64", python=sys.executable,
                    tools=dict(tools or {}), package_managers=dict(pkg or {}))
    return Provisioner(plat, work_root=tmp_path, on_output=lambda _line: None)


def _recipe(steps, *, detect=None, verify=None, slug="demo", workdir="demo", driver="demo"):
    return Recipe(slug=slug, driver=driver, workdir=workdir,
                  detect=detect if detect is not None else [Probe(kind="binary", name="nope-xyz")],
                  steps=list(steps), verify=list(verify or []))


def _temp_recipe_dir(tmp_path: Path, monkeypatch, body: str) -> Path:
    rdir = tmp_path / "recipes"
    rdir.mkdir(exist_ok=True)
    (rdir / "demo.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setenv("HIL_RECIPES_DIR", str(rdir))
    return rdir


class _FakeDaemon:
    """A one-shot JSON-RPC 'hello' server for the daemon probe."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(1.0)
                try:
                    conn.recv(4096)
                    reply = {"jsonrpc": "2.0", "id": 1,
                             "result": {"device": {"model": "ATK-Logic"}}}
                    conn.sendall((json.dumps(reply) + "\n").encode())
                except OSError:
                    pass

    def close(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass


# =========================================================================
# recipe model + validation
# =========================================================================
def test_parse_minimal_recipe():
    r = parse_recipe({"slug": "demo", "driver": "scpi"})
    assert r.slug == "demo" and r.driver == "scpi"
    assert r.steps == [] and r.detect == [] and r.verify == []


def test_parse_probe_shorthand_and_explicit():
    r = parse_recipe({
        "slug": "demo",
        "detect": ["yaml", {"python_import": {"module": "json"}}],
        "verify": [{"daemon": {"address": "logic://h:1"}}],
    })
    assert r.detect[0].kind == "python_import" and r.detect[0].module == "yaml"
    assert r.detect[1].module == "json"
    assert r.verify[0].kind == "daemon" and r.verify[0].address == "logic://h:1"


def test_parse_step_flags_and_defaults():
    r = parse_recipe({"slug": "demo", "steps": [
        {"id": "a", "pip": {"packages": ["x"]}, "optional": True},
        {"id": "b", "system": {"macos": {"brew": ["y"]}}, "heavy": True},
        {"id": "c", "shell": "echo hi", "needs": ["make"], "only_os": ["linux"]},
    ]})
    a, b, c = r.steps
    assert a.action == "pip" and a.optional and a.privileged is False
    assert b.action == "system" and b.heavy and b.privileged is True   # system defaults privileged
    assert c.action == "shell" and c.needs == ["make"] and c.only_os == ["linux"]
    assert a.label() == "a"                                            # falls back to id


def test_probe_labels_are_descriptive():
    assert Probe(kind="python_import", module="yaml").label() == "import yaml"
    assert Probe(kind="daemon", address="logic://h:1").label() == "daemon logic://h:1"
    assert Probe(kind="binary", name="qmake").label() == "binary qmake"
    assert Probe(kind="command", describe="custom").label() == "custom"


@pytest.mark.parametrize("bad", [
    {},                                                        # no slug
    {"slug": ""},                                              # empty slug
    {"slug": "Has Spaces"},                                    # invalid slug chars
    {"slug": "ok", "steps": [{"id": "x"}]},                    # step with no action
    {"slug": "ok", "steps": [{"pip": {}, "git": {}}]},         # two actions
    {"slug": "ok", "detect": [{"python_import": "a", "binary": "b"}]},  # two probe kinds
    {"slug": "ok", "steps": [{"id": "s", "shell": {"plan9": "x"}}]},    # bad platform key
])
def test_parse_recipe_rejects_invalid(bad):
    with pytest.raises(ProvisionError):
        parse_recipe(bad)


def test_bundled_recipes_load_and_are_well_formed():
    ppk2 = load_recipe_file(catalog.BUILTIN_RECIPES / "ppk2.yaml")
    atk = load_recipe_file(catalog.BUILTIN_RECIPES / "alientek-atk-logic.yaml")
    assert ppk2.slug == "ppk2" and ppk2.driver == "ppk2"
    assert any(s.action == "pip" for s in ppk2.steps)
    assert atk.slug == "alientek-atk-logic" and atk.driver == "logic"
    build = next(s for s in atk.steps if s.id == "build-daemon")
    assert build.heavy and build.optional                 # long compile: never implicit
    assert any(s.action == "git" for s in atk.steps)      # clones the sources


def test_debug_recipes_load_and_are_well_formed():
    probe_rs = load_recipe_file(catalog.BUILTIN_RECIPES / "probe-rs.yaml")
    assert probe_rs.slug == "probe-rs" and probe_rs.driver == "debug"
    assert probe_rs.license.startswith("MIT")             # license-compatible default
    cargo = next(s for s in probe_rs.steps if s.id == "cargo-install")
    assert cargo.heavy and cargo.optional                 # long compile: never implicit
    # Alternative backends are reachable by slug and carry NO driver, so
    # recipe_for(driver='debug') keeps resolving to probe-rs (last-wins safe).
    for slug, lic in (("openocd", "GPL-2.0"), ("esptool", "GPL-2.0"),
                      ("stlink", "BSD-3"), ("pyocd", "Apache-2.0"),
                      ("debug-gdb", "GPL-3.0")):
        r = load_recipe_file(catalog.BUILTIN_RECIPES / f"{slug}.yaml")
        assert r.slug == slug and r.driver == "" and r.license.startswith(lic)
        assert r.detect and r.verify                      # every recipe can be checked
    # esptool/pyocd are pure-Python (pip); openocd is a system package
    assert any(s.action == "pip"
               for s in load_recipe_file(catalog.BUILTIN_RECIPES / "esptool.yaml").steps)
    assert any(s.action == "system"
               for s in load_recipe_file(catalog.BUILTIN_RECIPES / "openocd.yaml").steps)


def test_only_one_recipe_claims_the_debug_driver():
    debug_recipes = [r.slug for r in catalog.load_recipes().values() if r.driver == "debug"]
    assert debug_recipes == ["probe-rs"]                  # no driver->recipe ambiguity


# =========================================================================
# platform detection
# =========================================================================
def test_detect_returns_known_os_and_tools():
    p = detect()
    assert p.os in (MACOS, LINUX, WINDOWS, platform_info.OTHER)
    assert p.arch and p.python and p.python_version
    assert "git" in p.tools and "qmake" in p.tools


def test_detect_cross_target_from_env(monkeypatch):
    monkeypatch.setenv("HIL_CROSS_TARGET", "android-arm64")
    assert detect().cross_target == "android-arm64"
    assert detect(cross_target="").cross_target == ""      # explicit override wins


def test_platform_pkg_manager_helpers():
    p = Platform(os=MACOS, package_managers={"brew": "/opt/bin/brew", "apt-get": None},
                 tools={"git": "/usr/bin/git"})
    assert p.has("git") and not p.has("make")
    assert p.preferred_pkg_manager() == "brew"
    assert p.pkg_managers_for_os() == ["brew"]             # apt-get is not a macOS manager
    assert "cross=" not in p.describe()
    assert "cross=t" in Platform(os=LINUX, cross_target="t").describe()


# =========================================================================
# engine: command builders
# =========================================================================
def test_pip_command_builder(tmp_path):
    p = _prov(tmp_path)
    cmd, argv = p._pip_command(Step(id="s", action="pip", pip={"packages": ["ppk2-api"]}))
    assert "install" in argv and "ppk2-api" in argv and "-m" in argv and "pip" in argv
    assert cmd == " ".join(argv)
    # the `extra` shorthand installs the HIL optional-dependency extra
    cmd2, argv2 = p._pip_command(Step(id="s", action="pip", pip={"extra": "ppk2"}))
    assert "hil-harness[ppk2]" in argv2


def test_git_command_clone_then_pull(tmp_path):
    p = _prov(tmp_path)
    step = Step(id="clone", action="git",
                git={"repo": "https://x/y.git", "dest": "src", "rev": "main", "depth": 1})
    root = tmp_path / "demo"
    _cmd, argv = p._git_command(step, root)
    assert argv[:2] == ["git", "clone"] and "--depth" in argv and "--branch" in argv
    assert str(root / "src") in argv
    (root / "src" / ".git").mkdir(parents=True)            # an existing clone -> pull
    _cmd2, argv2 = p._git_command(step, root)
    assert argv2[:2] == ["git", "-C"] and "pull" in argv2


def test_system_command_brew_is_not_privileged(tmp_path):
    p = _prov(tmp_path, pkg={"brew": "/opt/bin/brew"})
    step = Step(id="s", action="system", system={"macos": {"brew": ["libusb"]}})
    cmd, argv, privileged = p._system_command(step)
    assert argv[0] == "brew" and "install" in argv and "libusb" in argv
    assert privileged is False                             # brew needs no sudo


def test_system_command_apt_is_privileged_and_prepends_sudo(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda name: f"/usr/bin/{name}")
    p = _prov(tmp_path, os_name=LINUX, pkg={"apt-get": "/usr/bin/apt-get"})
    step = Step(id="s", action="system", system={"linux": {"apt-get": ["libfoo-dev"]}})
    _cmd, argv, privileged = p._system_command(step)
    assert privileged is True and argv[0] == "sudo" and "apt-get" in argv


def test_system_command_without_manager_is_none(tmp_path):
    p = _prov(tmp_path, os_name=LINUX, pkg={})
    step = Step(id="s", action="system", system={"linux": {"apt-get": ["x"]}})
    assert p._system_command(step) is None


# =========================================================================
# engine: gating + execution
# =========================================================================
def test_dry_run_executes_nothing(tmp_path):
    p = _prov(tmp_path)
    marker = tmp_path / "demo" / "made.txt"
    rep = p.provision(_recipe([Step(id="s", action="shell", shell=f"touch {marker}")]),
                      dry_run=True)
    assert rep.steps[0].status == "dry-run"
    assert not marker.exists()
    assert rep.verify == []                                # verify is skipped in dry-run


def test_shell_step_runs_then_verify_passes(tmp_path):
    p = _prov(tmp_path)
    rep = p.provision(_recipe(
        [Step(id="s", action="shell", shell="touch made.txt")],
        verify=[Probe(kind="binary", paths=["demo/made.txt"])],
    ))
    assert rep.steps[0].status == "ran"
    assert rep.verify[0].ok is True
    assert rep.satisfied is True


def test_privileged_step_gated_without_yes(tmp_path):
    p = _prov(tmp_path, os_name=LINUX, pkg={"apt-get": "/usr/bin/apt-get"})
    rep = p.provision(_recipe([Step(id="s", action="system", privileged=True,
                                    system={"linux": {"apt-get": ["x"]}})]), auto=True)
    assert rep.steps[0].status == "gated"
    assert "apt-get" in rep.steps[0].command
    assert any("privileged" in m for m in rep.messages)


def test_heavy_step_gated_in_auto_mode_but_runs_with_yes(tmp_path):
    gated = _prov(tmp_path).provision(
        _recipe([Step(id="build", action="shell", heavy=True, shell="make")]), auto=True)
    assert gated.steps[0].status == "gated"
    assert gated.steps[0].command == "make"                # preview shown, not executed

    ran = _prov(tmp_path).provision(
        _recipe([Step(id="build", action="shell", heavy=True, shell="echo built")]),
        auto=True, assume_yes=True)
    assert ran.steps[0].status == "ran"


def test_optional_failure_does_not_abort(tmp_path):
    rep = _prov(tmp_path).provision(_recipe([
        Step(id="bad", action="shell", optional=True, shell="exit 3"),
        Step(id="good", action="shell", shell="echo ok"),
    ]))
    assert rep.steps[0].status == "optional-failed"
    assert rep.steps[1].status == "ran"
    assert rep.ok is True
    assert any("optional step 'bad' failed" in m for m in rep.messages)


def test_hard_failure_aborts_remaining_steps(tmp_path):
    rep = _prov(tmp_path).provision(_recipe([
        Step(id="bad", action="shell", shell="exit 3"),
        Step(id="never", action="shell", shell="echo ok"),
    ]))
    assert rep.steps[0].status == "failed"
    assert rep.steps[1].status == "skipped"
    assert rep.ok is False


def test_only_os_and_needs_skip_cleanly(tmp_path):
    p = _prov(tmp_path, os_name=MACOS, tools={})
    rep = p.provision(_recipe([
        Step(id="os", action="shell", only_os=["linux"], shell="echo x"),
        Step(id="tool", action="shell", needs=["qmake"], shell="echo x"),
    ]))
    assert rep.steps[0].status == "skipped" and "linux" in rep.steps[0].detail
    assert rep.steps[1].status == "skipped" and "qmake" in rep.steps[1].detail


def test_already_satisfied_short_circuits(tmp_path):
    rep = _prov(tmp_path).provision(_recipe(
        [Step(id="s", action="shell", shell="echo x")],
        detect=[Probe(kind="python_import", module="yaml")],
        verify=[Probe(kind="python_import", module="yaml")],
    ))
    assert rep.satisfied is True
    assert rep.steps == []                                 # nothing ran
    assert any("already satisfied" in m for m in rep.messages)


# =========================================================================
# engine: probes
# =========================================================================
def test_python_import_probe(tmp_path):
    p = _prov(tmp_path)
    assert p.run_probe(Probe(kind="python_import", module="yaml")).ok is True
    assert p.run_probe(Probe(kind="python_import", module="nope_not_real_mod")).ok is False


def test_binary_probe_paths_and_which(tmp_path):
    (tmp_path / "tool.bin").write_text("x")
    p = _prov(tmp_path)
    assert p.run_probe(Probe(kind="binary", paths=["tool.bin"])).ok is True
    assert p.run_probe(Probe(kind="binary", name="definitely-not-on-path-xyz")).ok is False


def test_command_probe_respects_expect_rc(tmp_path):
    p = _prov(tmp_path)
    assert p.run_probe(Probe(kind="command", shell="exit 0")).ok is True
    assert p.run_probe(Probe(kind="command", shell="exit 2")).ok is False
    assert p.run_probe(Probe(kind="command", shell="exit 2", expect_rc=2)).ok is True


def test_daemon_probe_hello(tmp_path):
    d = _FakeDaemon()
    try:
        res = _prov(tmp_path).run_probe(
            Probe(kind="daemon", address=f"logic://127.0.0.1:{d.port}"))
        assert res.ok is True and "ATK-Logic" in res.detail
    finally:
        d.close()


def test_daemon_probe_unreachable(tmp_path):
    d = _FakeDaemon()
    port = d.port
    d.close()                                              # released -> connection refused
    assert _prov(tmp_path).run_probe(
        Probe(kind="daemon", address=f"logic://127.0.0.1:{port}")).ok is False


@pytest.mark.parametrize("addr,expected", [
    ("logic://127.0.0.1:5555", ("127.0.0.1", 5555)),
    ("127.0.0.1:1234", ("127.0.0.1", 1234)),
    ("logic://host", ("host", 5555)),
    ("host", ("host", 5555)),
])
def test_split_hostport(addr, expected):
    assert _split_hostport(addr) == expected


def test_split_hostport_bad_port():
    assert _split_hostport("logic://host:notaport") == ("", 0)


# =========================================================================
# catalog: discovery + resolution
# =========================================================================
def test_load_recipes_includes_bundled():
    assert {"ppk2", "alientek-atk-logic"} <= set(catalog.load_recipes())
    # the flash/debug toolchain recipes ship alongside them
    assert {"probe-rs", "openocd", "esptool", "stlink", "pyocd", "debug-gdb"} \
        <= set(catalog.load_recipes())


def test_resolve_targets_by_slug_driver_device_and_all():
    from hil.registry import load_registry
    recipes, reg = catalog.load_recipes(), load_registry()
    assert [r.slug for r in catalog.resolve_targets(["ppk2"], recipes=recipes, registry=reg)] == ["ppk2"]
    assert [r.slug for r in catalog.resolve_targets(["logic"], recipes=recipes, registry=reg)] == ["alientek-atk-logic"]
    assert [r.slug for r in catalog.resolve_targets(["nordic-ppk2"], recipes=recipes, registry=reg)] == ["ppk2"]
    assert {"ppk2", "alientek-atk-logic"} <= {r.slug for r in catalog.resolve_targets(["all"], recipes=recipes, registry=reg)}


def test_resolve_targets_unknown_raises():
    with pytest.raises(ProvisionError):
        catalog.resolve_targets(["does-not-exist"], recipes=catalog.load_recipes(), registry={})


def test_recipe_for_by_driver_and_device():
    assert catalog.recipe_for(driver="ppk2").slug == "ppk2"
    assert catalog.recipe_for(device_slug="nordic-ppk2").slug == "ppk2"
    assert catalog.recipe_for(driver="scpi") is None
    # driver 'debug' resolves to the license-compatible default backend
    assert catalog.recipe_for(driver="debug").slug == "probe-rs"
    assert catalog.recipe_for(device_slug="generic-cortex-m").slug == "probe-rs"


def test_resolve_targets_debug_tools_and_driver():
    from hil.registry import load_registry
    recipes, reg = catalog.load_recipes(), load_registry()
    # each tool recipe is reachable by its own slug
    for slug in ("probe-rs", "openocd", "esptool", "stlink", "pyocd", "debug-gdb"):
        assert [r.slug for r in catalog.resolve_targets([slug], recipes=recipes, registry=reg)] == [slug]
    # the driver name and a debug device both resolve to the default backend
    assert [r.slug for r in catalog.resolve_targets(["debug"], recipes=recipes, registry=reg)] == ["probe-rs"]
    assert [r.slug for r in catalog.resolve_targets(["generic-cortex-m"], recipes=recipes, registry=reg)] == ["probe-rs"]


def test_work_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HIL_PROVISION_WORK", str(tmp_path / "w"))
    assert catalog.work_root() == tmp_path / "w"


def test_third_party_dir_is_discovered():
    assert catalog.third_party_dir() is not None           # this repo ships third_party/


def test_recipes_env_dir_overrides_bundled(monkeypatch, tmp_path):
    override = tmp_path / "recipes"
    override.mkdir()
    (override / "ppk2.yaml").write_text(
        "slug: ppk2\ndriver: ppk2\ndescription: OVERRIDDEN\n", encoding="utf-8")
    monkeypatch.setenv("HIL_RECIPES_DIR", str(override))
    assert catalog.load_recipes()["ppk2"].description == "OVERRIDDEN"


# =========================================================================
# public API: status / setup_hint / ensure
# =========================================================================
def test_status_reports_every_recipe():
    reports = provision.status()
    assert {"ppk2", "alientek-atk-logic"} <= {r.recipe.slug for r in reports}
    assert all(isinstance(r.satisfied, bool) for r in reports)


def test_setup_hint_maps_driver_and_device():
    assert provision.setup_hint(driver="logic") == "hil setup alientek-atk-logic"
    assert provision.setup_hint(driver="ppk2") == "hil setup ppk2"
    assert provision.setup_hint(device_slug="nordic-ppk2") == "hil setup ppk2"
    assert provision.setup_hint(driver="scpi") == ""


def test_ensure_without_recipe_is_none():
    assert provision.ensure(driver="scpi") is None


def test_ensure_disabled_returns_hint_report(tmp_path, monkeypatch):
    _temp_recipe_dir(tmp_path, monkeypatch, """
        slug: demo
        driver: demo
        workdir: demo
        detect:
          - binary: { name: definitely-not-here-xyz }
        steps:
          - id: ping
            shell: "echo ping"
        verify:
          - python_import: yaml
    """)
    monkeypatch.delenv("HIL_AUTO_PROVISION", raising=False)
    rep = provision.ensure(driver="demo", work=tmp_path / "work")
    assert rep is not None and rep.satisfied is False
    assert any("hil setup demo" in m for m in rep.messages)


def test_ensure_auto_runs_unprivileged_steps(tmp_path, monkeypatch):
    _temp_recipe_dir(tmp_path, monkeypatch, """
        slug: demo
        driver: demo
        workdir: demo
        detect:
          - binary: { name: definitely-not-here-xyz }
        steps:
          - id: ping
            shell: "echo ping"
        verify:
          - python_import: yaml
    """)
    rep = provision.ensure(driver="demo", auto=True, work=tmp_path / "work",
                           on_output=lambda _line: None)
    assert rep is not None and rep.satisfied is True
    assert [s.status for s in rep.steps] == ["ran"]


def test_ensure_satisfied_is_none(tmp_path, monkeypatch):
    _temp_recipe_dir(tmp_path, monkeypatch, """
        slug: demo
        driver: demo
        detect:
          - python_import: yaml
        verify:
          - python_import: yaml
    """)
    assert provision.ensure(driver="demo", auto=True, work=tmp_path) is None


# =========================================================================
# connect() integration
# =========================================================================
def test_provision_is_part_of_public_surface():
    assert "provision" in hil.__all__
    assert hil.provision is provision


def test_annotate_missing_backend_preserves_type_and_is_idempotent():
    exc = TransportNotFoundError("ppk2", "ppk2", "ppk2-api")
    hil._annotate_missing_backend(exc, "ppk2", "")
    assert isinstance(exc, TransportNotFoundError)
    assert "hil setup ppk2" in str(exc)
    hil._annotate_missing_backend(exc, "ppk2", "")         # no duplicate hint
    assert str(exc).count("hil setup ppk2") == 1


def test_connect_missing_ppk2_backend_hints_setup(monkeypatch):
    monkeypatch.delenv("HIL_AUTO_PROVISION", raising=False)
    if provision.status(["ppk2"])[0].satisfied:
        pytest.skip("ppk2-api is installed; cannot exercise the missing-backend hint")
    with pytest.raises(HilError) as ei:
        hil.connect("ppk2:///dev/null")
    assert "hil setup ppk2" in str(ei.value)


# =========================================================================
# CLI: `hil setup` + `hil doctor`
# =========================================================================
def test_cli_setup_list(capsys):
    assert main(["setup", "--list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"ppk2", "alientek-atk-logic"} <= {r["slug"] for r in rows}

    assert main(["setup", "--list"]) == 0
    out = capsys.readouterr().out
    assert "provisioning recipes" in out and "ppk2" in out


def test_cli_setup_dry_run_json(capsys):
    assert main(["setup", "ppk2", "--dry-run", "--json"]) == 0
    reports = json.loads(capsys.readouterr().out)
    assert reports and reports[0]["slug"] == "ppk2"
    # nothing executed: every step is previewed, skipped, or gated
    assert all(s["status"] in ("dry-run", "skipped", "gated") for s in reports[0]["steps"])


def test_cli_setup_check_json(capsys):
    code = main(["setup", "--check", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {r["slug"] for r in payload} >= {"ppk2", "alientek-atk-logic"}
    assert code in (0, 1)                                  # 1 while a backend is missing


def test_cli_doctor_includes_provisioning(capsys):
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    prov = {p["slug"]: p for p in payload["provisioning"]}
    assert "ppk2" in prov
    assert {"slug", "ready", "license", "hint"} <= set(prov["ppk2"])
