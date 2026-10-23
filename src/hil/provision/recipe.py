"""Declarative provisioning recipes.

A *recipe* describes how to make one optional, third-party device backend
available on this host: how to **detect** whether it is already satisfied, the
**steps** to fetch/build it on demand, and how to **verify** the result. Recipes
are plain YAML so a lab can add or override one without touching HIL code (drop
a file in ``third_party/recipes/`` or point ``$HIL_RECIPES_DIR`` at a folder).

Two shapes of backend are covered:

* **Python packages** (e.g. the GPLv2 ``ppk2-api``) — a ``pip`` step plus an
  optional privileged ``system`` step for native runtime libs (``libusb``).
* **Native builds** (e.g. the GPLv3 ``atk-logic`` capture daemon) — ``system``
  deps, a ``git`` clone, ``shell`` build steps (qmake/make, cross-compile aware),
  and a ``daemon``/``binary`` verify probe.

The model is intentionally small and stdlib-only; the engine
(:mod:`hil.provision.engine`) interprets it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import ProvisionError

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# A detect/verify probe is one of these kinds.
PROBE_KINDS = ("python_import", "binary", "command", "daemon")
# A provisioning step carries exactly one of these action payloads.
STEP_ACTIONS = ("pip", "system", "git", "shell")
# OS families a per-platform payload may be keyed by (plus ``default``).
PLATFORM_KEYS = ("default", "macos", "linux", "windows", "other")


@dataclass
class Probe:
    """A single yes/no check used by ``detect`` and ``verify``.

    ``kind`` selects the interpretation:

    * ``python_import`` — ``module`` imports cleanly.
    * ``binary`` — ``name`` is on PATH, or one of ``paths`` exists/executable.
    * ``command`` — ``argv`` (or ``shell``) exits with ``expect_rc`` (default 0).
    * ``daemon`` — a JSON-RPC ``hello`` succeeds at ``address`` (a ``logic://``
      or ``host:port`` capture daemon); proves the built daemon actually runs.
    """

    kind: str
    module: str = ""
    name: str = ""
    paths: list[str] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)
    shell: str = ""
    expect_rc: int = 0
    address: str = ""
    describe: str = ""

    def label(self) -> str:
        if self.describe:
            return self.describe
        if self.kind == "python_import":
            return f"import {self.module}"
        if self.kind == "binary":
            return f"binary {self.name or (self.paths[0] if self.paths else '?')}"
        if self.kind == "command":
            return f"command {' '.join(self.argv) or self.shell}"
        if self.kind == "daemon":
            return f"daemon {self.address}"
        return self.kind


@dataclass
class Step:
    """One provisioning action, run in order.

    Exactly one of ``pip`` / ``system`` / ``git`` / ``shell`` is populated;
    ``action`` names which. ``privileged`` steps (system package installs that
    may need sudo) are gated behind explicit confirmation. ``heavy`` steps (a
    long native compile) are likewise gated in auto mode, so an implicit
    ``connect()`` never stalls on a multi-minute build. ``optional`` steps warn
    instead of aborting on failure (e.g. decode-lib build — capture still works
    without it). ``only_os`` / ``needs`` skip a step cleanly where it does not
    apply (wrong OS, or a required tool like ``qmake`` is absent).
    """

    id: str
    name: str = ""
    action: str = ""
    privileged: bool = False
    optional: bool = False
    heavy: bool = False                 # long native build: skipped in auto mode
    only_os: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    # payloads
    pip: dict[str, Any] = field(default_factory=dict)
    system: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    shell: Any = ""            # str, or {platform: str}

    def label(self) -> str:
        return self.name or self.id


@dataclass
class Recipe:
    """How to provision one third-party backend."""

    slug: str
    driver: str = ""
    description: str = ""
    license: str = ""
    homepage: str = ""
    workdir: str = ""                       # relative to the provision work root
    detect: list[Probe] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    verify: list[Probe] = field(default_factory=list)
    notes: str = ""
    source: str = ""                        # file it was loaded from

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "driver": self.driver,
            "description": self.description,
            "license": self.license,
            "steps": [s.id for s in self.steps],
            "detect": [p.label() for p in self.detect],
        }


# -- parsing ----------------------------------------------------------------
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _parse_probe(raw: Any, where: str) -> Probe:
    """Parse a probe from ``{kind: ...}`` shorthand or an explicit ``kind:`` map."""
    if isinstance(raw, str):
        raw = {"python_import": raw}
    if not isinstance(raw, dict) or not raw:
        raise ProvisionError(f"{where}: probe must be a non-empty mapping, got {raw!r}")

    if "kind" in raw:
        kind = str(raw["kind"])
        body = raw
    else:
        keys = [k for k in raw if k in PROBE_KINDS]
        if len(keys) != 1:
            raise ProvisionError(
                f"{where}: probe needs exactly one of {PROBE_KINDS} (or a 'kind'), got {list(raw)}"
            )
        kind = keys[0]
        body = raw[keys[0]] if isinstance(raw[keys[0]], dict) else {"value": raw[keys[0]]}
        # shorthand scalar: `- python_import: ppk2_api`
        if "value" in body:
            body = {("module" if kind == "python_import" else "name"): body["value"]}

    if kind == "command":
        argv = [str(a) for a in _as_list(body.get("argv"))]
        return Probe(kind=kind, argv=argv, shell=str(body.get("shell", "")),
                     expect_rc=int(body.get("expect_rc", 0)),
                     describe=str(raw.get("describe", "")))
    if kind == "daemon":
        return Probe(kind=kind, address=str(body.get("address", "") or raw.get("address", "")),
                     describe=str(raw.get("describe", "")))
    if kind == "binary":
        return Probe(kind=kind, name=str(body.get("name", "")),
                     paths=[str(p) for p in _as_list(body.get("paths"))],
                     describe=str(raw.get("describe", "")))
    if kind == "python_import":
        return Probe(kind=kind, module=str(body.get("module", "")),
                     describe=str(raw.get("describe", "")))
    raise ProvisionError(f"{where}: unknown probe kind '{kind}'")


def _parse_step(raw: Any, index: int) -> Step:
    if not isinstance(raw, dict):
        raise ProvisionError(f"step {index}: must be a mapping, got {type(raw).__name__}")
    actions = [k for k in raw if k in STEP_ACTIONS]
    if len(actions) != 1:
        raise ProvisionError(
            f"step {index}: needs exactly one action of {STEP_ACTIONS}, got {actions or list(raw)}"
        )
    action = actions[0]
    sid = str(raw.get("id") or raw.get("name") or f"{action}-{index}")
    shell = raw.get("shell", "")
    if isinstance(shell, dict):
        bad = [k for k in shell if k not in PLATFORM_KEYS]
        if bad:
            raise ProvisionError(f"step {index} ('{sid}'): shell keys must be in {PLATFORM_KEYS}, got {bad}")
    return Step(
        id=sid,
        name=str(raw.get("name", "")),
        action=action,
        privileged=bool(raw.get("privileged", action == "system")),
        optional=bool(raw.get("optional", False)),
        heavy=bool(raw.get("heavy", False)),
        only_os=[str(o) for o in _as_list(raw.get("only_os"))],
        needs=[str(t) for t in _as_list(raw.get("needs"))],
        pip=dict(raw.get("pip") or {}),
        system=dict(raw.get("system") or {}),
        git=dict(raw.get("git") or {}),
        shell=shell,
    )


def parse_recipe(data: Any, source: str = "") -> Recipe:
    """Validate a raw recipe mapping into a :class:`Recipe`."""
    if not isinstance(data, dict):
        raise ProvisionError(f"{source or 'recipe'}: expected a mapping at top level")
    slug = str(data.get("slug", "")).strip()
    if not slug or not SLUG_RE.match(slug):
        raise ProvisionError(
            f"{source or 'recipe'}: 'slug' is required and must be lowercase/digits/hyphens"
        )
    return Recipe(
        slug=slug,
        driver=str(data.get("driver", "")),
        description=str(data.get("description", "")),
        license=str(data.get("license", "")),
        homepage=str(data.get("homepage", "")),
        workdir=str(data.get("workdir", "")),
        detect=[_parse_probe(p, f"{slug}.detect[{i}]")
                for i, p in enumerate(_as_list(data.get("detect")), 1)],
        steps=[_parse_step(s, i) for i, s in enumerate(_as_list(data.get("steps")), 1)],
        verify=[_parse_probe(p, f"{slug}.verify[{i}]")
                for i, p in enumerate(_as_list(data.get("verify")), 1)],
        notes=str(data.get("notes", "")),
        source=source,
    )


def load_recipe_file(path: str | Path) -> Recipe:
    """Parse and validate one recipe YAML file."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProvisionError(f"{path.name}: invalid YAML: {exc}") from exc
    return parse_recipe(data, source=str(path))
