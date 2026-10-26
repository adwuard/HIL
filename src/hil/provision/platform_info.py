"""Host platform detection for on-demand provisioning.

Provisioning recipes are declarative and *per-platform*: a step may install
packages with ``brew``/``apt``/``choco``, or run a shell build only where a
toolchain exists. This module answers the two questions the engine needs:

* **where are we** — OS family, machine arch, Python;
* **what is already here** — which package managers and build tools are on PATH.

Everything is standard library and side-effect free: detection only *looks*, it
never installs. Callers pass the resulting :class:`Platform` to the engine so a
single detection can be reused (and so tests can inject a fake).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field

# OS families the recipes know about.
MACOS = "macos"
LINUX = "linux"
WINDOWS = "windows"
OTHER = "other"

# Package managers, in the order preferred per OS family.
_PKG_MANAGERS = (
    "brew", "port",            # macOS
    "apt-get", "dnf", "yum", "pacman", "zypper", "apk",  # Linux
    "choco", "winget", "scoop",                          # Windows
)

# Build / VCS tools a native recipe may need.
_TOOLS = (
    "git", "make", "cmake", "ninja", "qmake", "qmake6",
    "pkg-config", "gcc", "g++", "clang", "python3", "pip3", "uv",
)


def os_family() -> str:
    """Return ``macos`` / ``linux`` / ``windows`` / ``other``."""
    if sys.platform == "darwin":
        return MACOS
    if sys.platform.startswith("linux"):
        return LINUX
    if os.name == "nt":
        return WINDOWS
    return OTHER


@dataclass
class Platform:
    """A snapshot of the host relevant to provisioning.

    ``tools`` and ``package_managers`` map a program name to its resolved PATH
    location (or ``None`` when absent) so the engine can both check presence and
    report *what* it would run.
    """

    os: str = OTHER
    arch: str = ""
    python: str = ""
    python_version: str = ""
    # False in uv-managed virtualenvs (which ship no pip module); the engine then
    # installs with `uv pip` instead of `python -m pip`, so a cold start works.
    has_pip: bool = True
    is_64bit: bool = True
    package_managers: dict[str, str | None] = field(default_factory=dict)
    tools: dict[str, str | None] = field(default_factory=dict)
    # A cross-compile target explicitly requested via the environment, e.g.
    # HIL_CROSS_TARGET=android-arm64. Empty means "build for this host".
    cross_target: str = ""

    # -- convenience ------------------------------------------------------
    def tool(self, name: str) -> str | None:
        """Resolved path of a tool, or ``None`` if not on PATH."""
        return self.tools.get(name)

    def has(self, name: str) -> bool:
        return bool(self.tools.get(name))

    def has_pkg_manager(self, name: str) -> bool:
        return bool(self.package_managers.get(name))

    def preferred_pkg_manager(self) -> str | None:
        """The first package manager available on this host (if any)."""
        for name in _PKG_MANAGERS:
            if self.package_managers.get(name):
                return name
        return None

    def pkg_managers_for_os(self) -> list[str]:
        """Package managers that make sense on this OS and are present."""
        if self.os == MACOS:
            order = ("brew", "port")
        elif self.os == LINUX:
            order = ("apt-get", "dnf", "yum", "pacman", "zypper", "apk")
        elif self.os == WINDOWS:
            order = ("choco", "winget", "scoop")
        else:
            order = _PKG_MANAGERS
        return [m for m in order if self.package_managers.get(m)]

    def describe(self) -> str:
        cross = f", cross={self.cross_target}" if self.cross_target else ""
        return f"{self.os}/{self.arch}, Python {self.python_version}{cross}"


def detect(*, cross_target: str | None = None) -> Platform:
    """Probe the host once and return a :class:`Platform`.

    ``cross_target`` defaults to ``$HIL_CROSS_TARGET`` (e.g. ``android-arm64``);
    when set, native build steps may select a cross toolchain instead of the
    host one.
    """
    machine = (os.environ.get("HIL_ARCH") or sysconfig.get_platform() or "").lower()
    if "arm64" in machine or "aarch64" in machine:
        arch = "arm64"
    elif "x86_64" in machine or "amd64" in machine:
        arch = "x86_64"
    else:
        arch = machine.split("-")[-1] or "unknown"

    return Platform(
        os=os_family(),
        arch=arch,
        python=sys.executable or "python3",
        python_version=".".join(str(p) for p in sys.version_info[:3]),
        has_pip=importlib.util.find_spec("pip") is not None,
        is_64bit=sys.maxsize > 2**32,
        package_managers={m: shutil.which(m) for m in _PKG_MANAGERS},
        tools={t: shutil.which(t) for t in _TOOLS},
        cross_target=(cross_target if cross_target is not None
                      else os.environ.get("HIL_CROSS_TARGET", "")),
    )
