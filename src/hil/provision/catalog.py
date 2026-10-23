"""Recipe discovery and device/driver -> recipe resolution.

Recipes ship inside the package (``hil/provision/recipes/``) so ``hil setup``
works from an installed wheel, and are *also* read from a visible
``third_party/recipes/`` folder and ``$HIL_RECIPES_DIR`` so a lab can add or
override one without editing HIL. Later sources win on slug collision (same
merge order the device registry uses).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import ProvisionError
from .recipe import Recipe, load_recipe_file

# Bundled recipes (shipped in the wheel).
BUILTIN_RECIPES = Path(__file__).parent / "recipes"
RECIPES_ENV = "HIL_RECIPES_DIR"
WORK_ENV = "HIL_PROVISION_WORK"


def _repo_third_party() -> Path | None:
    """Find a ``third_party`` dir by walking up from CWD and from this package."""
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for parent in (start, *start.parents):
            cand = parent / "third_party"
            if cand.is_dir():
                return cand
            if (parent / ".git").exists():
                break
    return None


def third_party_dir() -> Path | None:
    """The visible ``third_party/`` integration folder, if present."""
    return _repo_third_party()


def work_root() -> Path:
    """Where clones/builds/downloads land.

    ``$HIL_PROVISION_WORK`` wins; else ``<repo>/third_party/work``; else a
    per-user cache dir. Created lazily by the engine, not here.
    """
    env = os.environ.get(WORK_ENV)
    if env:
        return Path(env).expanduser()
    tp = _repo_third_party()
    if tp is not None:
        return tp / "work"
    return Path.home() / ".cache" / "hil" / "third_party" / "work"


def recipe_dirs(extra_dirs: tuple[Path, ...] = ()) -> list[Path]:
    """All recipe dirs in merge order (later overrides earlier)."""
    dirs: list[Path] = [BUILTIN_RECIPES]
    tp = _repo_third_party()
    if tp is not None:
        dirs.append(tp / "recipes")
    env = os.environ.get(RECIPES_ENV)
    if env:
        dirs.append(Path(env).expanduser())
    dirs.extend(Path(d) for d in extra_dirs)
    return dirs


def load_recipes(extra_dirs: tuple[Path, ...] = ()) -> dict[str, Recipe]:
    """Load every recipe, merged across the discovery dirs (later wins)."""
    recipes: dict[str, Recipe] = {}
    for directory in recipe_dirs(extra_dirs):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.y*ml")):
            recipe = load_recipe_file(path)
            recipes[recipe.slug] = recipe
    return recipes


def _by_driver(recipes: dict[str, Recipe]) -> dict[str, Recipe]:
    return {r.driver: r for r in recipes.values() if r.driver}


def resolve_targets(names: list[str], *, recipes: dict[str, Recipe] | None = None,
                    registry: dict | None = None) -> list[Recipe]:
    """Map target names to recipes.

    A target may be a recipe slug, a driver name (``logic``), a device slug
    (``nordic-ppk2`` -> the ppk2 recipe via its driver), or ``all``. Unknown
    names raise :class:`ProvisionError` listing what is available.
    """
    recipes = recipes if recipes is not None else load_recipes()
    if registry is None:
        try:
            from ..registry import load_registry
            registry = load_registry()
        except Exception:  # pragma: no cover - registry is core, but stay robust
            registry = {}
    by_driver = _by_driver(recipes)
    chosen: dict[str, Recipe] = {}

    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        if name.lower() in ("all", "*"):
            chosen.update({r.slug: r for r in recipes.values()})
            continue
        low = name.lower()
        if low in recipes:                       # exact recipe slug
            chosen[recipes[low].slug] = recipes[low]
            continue
        if low in by_driver:                     # driver name
            chosen[by_driver[low].slug] = by_driver[low]
            continue
        device = registry.get(low)               # device slug -> its driver's recipe
        if device is not None:
            drv = getattr(device, "driver", "") or ""
            if low in recipes:
                chosen[recipes[low].slug] = recipes[low]
            elif drv in by_driver:
                chosen[by_driver[drv].slug] = by_driver[drv]
            else:
                raise ProvisionError(
                    f"device '{low}' (driver '{drv}') has no provisioning recipe "
                    f"— it likely needs no third-party backend"
                )
            continue
        raise ProvisionError(
            f"unknown provisioning target '{name}' (recipes: {', '.join(sorted(recipes))}; "
            "or a device slug / driver name / 'all')"
        )
    return list(chosen.values())


def recipe_for(device_slug: str = "", driver: str = "", *,
               recipes: dict[str, Recipe] | None = None) -> Recipe | None:
    """The recipe that would provision a device/driver, or ``None``."""
    recipes = recipes if recipes is not None else load_recipes()
    if device_slug and device_slug in recipes:
        return recipes[device_slug]
    by_driver = _by_driver(recipes)
    if driver and driver in by_driver:
        return by_driver[driver]
    if device_slug:
        try:
            from ..registry import load_registry
            dev = load_registry().get(device_slug)
        except Exception:  # pragma: no cover
            dev = None
        drv = getattr(dev, "driver", "") or ""
        if drv in by_driver:
            return by_driver[drv]
    return None
