"""On-demand provisioning of optional, third-party device backends.

HIL's MIT core works out of the box for the common case (SCPI over
``tcp://``/``serial://``/``visa://``). A few devices need an *optional* backend
that HIL deliberately does not bundle:

* **Nordic PPK2** — the GPLv2 ``ppk2-api`` Python package (+ ``libusb``).
* **ALIENTEK ATK-Logic** — driven in-process by HIL's vendored MIT core; the only
  optional deps are ``pyusb`` + the native ``libusb`` runtime (no daemon, no build).

This package turns "install the right thing for the device you just plugged in"
into a declarative, testable, cross-platform flow::

    hil setup                 # show what each backend needs / whether it's ready
    hil setup ppk2            # fetch + install the PPK2 backend
    hil setup alientek-atk-logic --yes   # ...and allow the privileged libusb install
    hil setup logic --dry-run # print the plan without running anything

Programmatic entry points: :func:`provision`, :func:`status`, and :func:`ensure`
(the on-demand hook ``hil.connect`` uses when ``HIL_AUTO_PROVISION`` is set).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from ..errors import ProvisionError
from .catalog import (
    BUILTIN_RECIPES,
    load_recipes,
    recipe_for,
    resolve_targets,
    third_party_dir,
    work_root,
)
from .engine import (
    ProbeResult,
    Provisioner,
    ProvisionReport,
    StepResult,
)
from .platform_info import Platform, detect
from .recipe import Probe, Recipe, Step, load_recipe_file, parse_recipe

__all__ = [
    "BUILTIN_RECIPES",
    "Platform",
    "Probe",
    "ProbeResult",
    "ProvisionError",
    "ProvisionReport",
    "Provisioner",
    "Recipe",
    "Step",
    "StepResult",
    "detect",
    "ensure",
    "load_recipe_file",
    "load_recipes",
    "parse_recipe",
    "provision",
    "recipe_for",
    "resolve_targets",
    "setup_hint",
    "status",
    "third_party_dir",
    "work_root",
]


def _provisioner(platform: Platform | None, on_output: Callable[[str], None] | None,
                 work: Path | None) -> Provisioner:
    return Provisioner(platform or detect(), work_root=work or work_root(),
                       on_output=on_output or (lambda _line: None))


def status(targets: Sequence[str] | None = None, *, platform: Platform | None = None,
           work: Path | None = None) -> list[ProvisionReport]:
    """Report whether each recipe's backend is already satisfied (no side effects).

    With no ``targets``, reports on every discovered recipe.
    """
    recipes = load_recipes()
    if targets:
        chosen = resolve_targets(list(targets), recipes=recipes)
    else:
        chosen = list(recipes.values())
    prov = _provisioner(platform, None, work)
    out: list[ProvisionReport] = []
    for recipe in chosen:
        detect_res = prov.check(recipe)
        satisfied = bool(detect_res) and all(p.ok for p in detect_res)
        out.append(ProvisionReport(recipe=recipe, satisfied=satisfied, detect=detect_res,
                                   ok=True))
    return out


def provision(targets: Sequence[str], *, dry_run: bool = False, assume_yes: bool = False,
              auto: bool = False, platform: Platform | None = None,
              on_output: Callable[[str], None] | None = None,
              work: Path | None = None) -> list[ProvisionReport]:
    """Resolve ``targets`` and run their recipes. Returns one report per recipe."""
    recipes = load_recipes()
    chosen = resolve_targets(list(targets), recipes=recipes)
    if not chosen:
        raise ProvisionError("no provisioning targets matched")
    prov = _provisioner(platform, on_output, work)
    return [prov.provision(r, dry_run=dry_run, assume_yes=assume_yes, auto=auto)
            for r in chosen]


def setup_hint(device_slug: str = "", driver: str = "") -> str:
    """The actionable ``hil setup ...`` command for a device/driver, if any."""
    recipe = recipe_for(device_slug=device_slug, driver=driver)
    if recipe is None:
        return ""
    return f"hil setup {recipe.slug}"


def ensure(*, device_slug: str = "", driver: str = "", auto: bool | None = None,
           platform: Platform | None = None,
           on_output: Callable[[str], None] | None = None,
           work: Path | None = None) -> ProvisionReport | None:
    """On-demand hook: make a device's backend available if a recipe exists.

    Returns ``None`` when there is nothing to provision (no recipe, or already
    satisfied). Otherwise, if provisioning is enabled (``auto`` or
    ``$HIL_AUTO_PROVISION``), runs the **non-privileged** steps and returns the
    report; privileged native builds are left to an explicit ``hil setup --yes``.
    Never raises for a missing backend — the caller decides how to surface it.
    """
    import os

    recipe = recipe_for(device_slug=device_slug, driver=driver)
    if recipe is None:
        return None
    prov = _provisioner(platform, on_output, work)
    if prov.is_satisfied(recipe):
        return None
    enabled = auto if auto is not None else os.environ.get(
        "HIL_AUTO_PROVISION", "").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        report = ProvisionReport(recipe=recipe, satisfied=False, ok=True)
        report.messages.append(f"backend not provisioned — run: {setup_hint(recipe.slug)}")
        return report
    return prov.provision(recipe, auto=True)
