"""The provisioning engine: detect -> provision -> verify.

The engine interprets a :class:`~hil.provision.recipe.Recipe` against a
:class:`~hil.provision.platform_info.Platform`:

* :func:`Provisioner.check` runs the recipe's ``detect`` probes — is the backend
  already usable?
* :func:`Provisioner.provision` runs the ``steps`` on demand (pip / system /
  git / shell), streaming output, honouring **dry-run** and the privilege gate.
* :func:`Provisioner.verify` runs the ``verify`` probes to confirm success.

Safety rules baked in:

* **Privileged steps** (system package installs that may need sudo) never run
  unless ``assume_yes`` is set; otherwise they are reported as *gated* with the
  exact command to re-run. ``HIL_AUTO_PROVISION`` auto-runs only *non-privileged*
  steps.
* **Heavy steps** (a long native compile) are gated in auto mode too, so an
  implicit ``connect()`` never starts a multi-minute build unannounced.
* **Dry-run** prints every command without executing anything.
* A failing step aborts the run unless it is marked ``optional``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..errors import ProvisionError
from .platform_info import LINUX, MACOS, WINDOWS, Platform, detect
from .recipe import Probe, Recipe, Step

Output = Callable[[str], None]

# System package managers that normally require root.
_SUDO_MANAGERS = {"apt-get", "dnf", "yum", "pacman", "zypper", "apk"}
_INSTALL_FLAGS = {
    "brew": ["install"],
    "port": ["install"],
    "apt-get": ["install", "-y"],
    "dnf": ["install", "-y"],
    "yum": ["install", "-y"],
    "pacman": ["-S", "--noconfirm"],
    "zypper": ["--non-interactive", "install"],
    "apk": ["add"],
    "choco": ["install", "-y"],
    "winget": ["install", "--accept-package-agreements", "--accept-source-agreements"],
    "scoop": ["install"],
}


def _default_output(line: str) -> None:
    print(line, flush=True)


@dataclass
class ProbeResult:
    probe: Probe
    ok: bool
    detail: str = ""


@dataclass
class StepResult:
    step: Step
    status: str                      # ran | skipped | gated | dry-run | failed | optional-failed
    detail: str = ""
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.status not in ("failed",)


@dataclass
class ProvisionReport:
    recipe: Recipe
    satisfied: bool
    detect: list[ProbeResult] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    verify: list[ProbeResult] = field(default_factory=list)
    ok: bool = True
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slug": self.recipe.slug,
            "satisfied": self.satisfied,
            "ok": self.ok,
            "detect": [{"check": p.probe.label(), "ok": p.ok, "detail": p.detail}
                       for p in self.detect],
            "steps": [{"id": s.step.id, "status": s.status, "detail": s.detail,
                       "command": s.command} for s in self.steps],
            "verify": [{"check": p.probe.label(), "ok": p.ok, "detail": p.detail}
                       for p in self.verify],
            "messages": self.messages,
        }

    def human(self) -> str:
        head = f"{self.recipe.slug}: {'ready' if self.satisfied else 'not provisioned'}"
        lines = [head]
        for s in self.steps:
            lines.append(f"  [{s.status:>9}] {s.step.label()}"
                         + (f" — {s.detail}" if s.detail else ""))
            if s.command and s.status in ("dry-run", "gated"):
                lines.append(f"             $ {s.command}")
        for p in self.verify:
            lines.append(f"  verify {'ok ' if p.ok else 'FAIL'} {p.probe.label()}"
                         + (f" — {p.detail}" if p.detail else ""))
        lines.extend(f"  note: {m}" for m in self.messages)
        return "\n".join(lines)


class Provisioner:
    """Runs recipes against a platform. Construct once, reuse."""

    def __init__(self, platform: Platform | None = None, *,
                 work_root: Path | None = None, on_output: Output = _default_output) -> None:
        self.platform = platform or detect()
        self.work_root = Path(work_root) if work_root else None
        self.out = on_output

    # -- detection --------------------------------------------------------
    def _recipe_root(self, recipe: Recipe) -> Path:
        base = self.work_root or Path(os.environ.get("HIL_PROVISION_WORK", ".hil-provision"))
        return base / (recipe.workdir or recipe.slug)

    def run_probe(self, probe: Probe) -> ProbeResult:
        if probe.kind == "python_import":
            try:
                found = importlib.util.find_spec(probe.module) is not None
            except (ImportError, ValueError, ModuleNotFoundError):
                found = False
            return ProbeResult(probe, found, "" if found else f"{probe.module} not importable")

        if probe.kind == "binary":
            if probe.name and shutil.which(probe.name):
                return ProbeResult(probe, True, shutil.which(probe.name) or "")
            for p in probe.paths:
                pp = self._resolve(p)
                if pp.exists():
                    return ProbeResult(probe, True, str(pp))
            return ProbeResult(probe, False, "not found")

        if probe.kind == "command":
            rc, tail = self._exec_probe(probe)
            ok = rc == probe.expect_rc
            return ProbeResult(probe, ok, f"rc={rc}" + (f" {tail}" if not ok and tail else ""))

        if probe.kind == "daemon":
            ok, detail = self._probe_daemon(probe.address)
            return ProbeResult(probe, ok, detail)

        return ProbeResult(probe, False, f"unknown probe kind '{probe.kind}'")

    def check(self, recipe: Recipe) -> list[ProbeResult]:
        probes = recipe.detect or recipe.verify
        return [self.run_probe(p) for p in probes]

    def is_satisfied(self, recipe: Recipe) -> bool:
        results = self.check(recipe)
        return bool(results) and all(r.ok for r in results)

    def verify(self, recipe: Recipe) -> list[ProbeResult]:
        return [self.run_probe(p) for p in recipe.verify]

    # -- provisioning -----------------------------------------------------
    def provision(self, recipe: Recipe, *, dry_run: bool = False,
                  assume_yes: bool = False, auto: bool = False) -> ProvisionReport:
        """Run a recipe's steps. Returns a report; never raises for step failures."""
        report = ProvisionReport(recipe=recipe, satisfied=self.is_satisfied(recipe))
        if report.satisfied:
            report.messages.append("already satisfied — nothing to do")
            report.verify = self.verify(recipe)
            return report

        auto = auto or _env_flag("HIL_AUTO_PROVISION")
        root = self._recipe_root(recipe)
        aborted = False
        for step in recipe.steps:
            if aborted:
                report.steps.append(StepResult(step, "skipped", "earlier step failed"))
                continue
            res = self._run_step(step, recipe, root, dry_run=dry_run,
                                 assume_yes=assume_yes, auto=auto)
            report.steps.append(res)
            if res.status == "failed":
                aborted = True
            elif res.status == "optional-failed":
                report.messages.append(f"optional step '{step.id}' failed: {res.detail}")
            elif res.status == "gated":
                report.messages.append(res.detail)

        report.verify = self.verify(recipe) if not dry_run else []
        report.satisfied = bool(report.verify) and all(p.ok for p in report.verify)
        report.ok = not aborted
        if not dry_run and not report.satisfied and report.ok:
            report.messages.append(
                f"steps completed but verification did not pass — see `hil setup {recipe.slug}`"
            )
        return report

    def _run_step(self, step: Step, recipe: Recipe, root: Path, *,
                  dry_run: bool, assume_yes: bool, auto: bool) -> StepResult:
        # OS gate
        if step.only_os and self.platform.os not in step.only_os:
            return StepResult(step, "skipped", f"only for {','.join(step.only_os)}")
        # required tools gate (e.g. cannot build without qmake)
        missing = [t for t in step.needs if not self.platform.has(t)]
        if missing:
            detail = f"missing tool(s): {', '.join(missing)}"
            if step.optional:
                return StepResult(step, "optional-failed", detail)
            return StepResult(step, "skipped", detail)
        # heavy gate: never start a long native compile implicitly (auto mode)
        if step.heavy and auto and not assume_yes and not dry_run:
            return StepResult(
                step, "gated",
                "long native build skipped in auto mode — run "
                f"`hil setup {recipe.slug}` to compile it explicitly",
                self._preview(step, root),
            )

        if step.action == "pip":
            cmd, argv = self._pip_command(step)
            return self._execute(step, argv, cmd, root, dry_run, privileged=False)

        if step.action == "system":
            built = self._system_command(step)
            if built is None:
                return StepResult(step, "skipped",
                                  "no supported package manager on this host")
            cmd, argv, privileged = built
            if privileged and not assume_yes:
                hint = ("privileged install skipped — re-run with --yes to allow "
                        f"(auto mode runs only unprivileged steps): $ {cmd}")
                if auto or dry_run:
                    return StepResult(step, "gated" if not dry_run else "dry-run", hint, cmd)
                return StepResult(step, "gated", hint, cmd)
            return self._execute(step, argv, cmd, root, dry_run, privileged=privileged)

        if step.action == "git":
            cmd, argv = self._git_command(step, root)
            return self._execute(step, argv, cmd, root, dry_run, privileged=False)

        if step.action == "shell":
            script = self._shell_script(step)
            if not script:
                return StepResult(step, "skipped", f"no shell command for {self.platform.os}")
            return self._execute(step, None, script, root, dry_run,
                                 privileged=step.privileged, shell=True)

        return StepResult(step, "skipped", f"unknown action '{step.action}'")

    # -- command builders -------------------------------------------------
    def _pip_command(self, step: Step) -> tuple[str, list[str]]:
        pkgs = [str(p) for p in step.pip.get("packages", []) if p]
        extra = str(step.pip.get("extra", ""))
        if not pkgs and extra:
            pkgs = [f"hil-harness[{extra}]"]
        index = str(step.pip.get("index_url", ""))
        argv = [*self._pip_installer(), *pkgs]
        if index:
            argv += ["--index-url", index]
        return " ".join(argv), argv

    def _pip_installer(self) -> list[str]:
        """The ``pip install`` argv prefix that works in *this* environment.

        Normally ``<python> -m pip install``. uv-managed virtualenvs ship without
        the pip module, so a cold start would die with "No module named pip";
        when the target interpreter has no pip we fall back to
        ``uv pip install --python <python>`` (installs into the same env, no pip
        needed). Falls back to ``-m pip`` if neither is usable so the error the
        user sees is still the familiar one.
        """
        py = self.platform.python or "python3"
        if getattr(self.platform, "has_pip", True):
            return [py, "-m", "pip", "install"]
        uv = self.platform.tool("uv") or shutil.which("uv")
        if uv:
            return [uv, "pip", "install", "--python", py]
        return [py, "-m", "pip", "install"]

    def _system_command(self, step: Step) -> tuple[str, list[str], bool] | None:
        mgrs = self.platform.pkg_managers_for_os()
        if not mgrs:
            return None
        spec = step.system.get(self.platform.os) or step.system.get("default") or {}
        # spec: {manager: [packages]} OR [packages] (uses the preferred manager)
        manager = None
        packages: list[str] = []
        if isinstance(spec, dict):
            for m in mgrs:
                if spec.get(m):
                    manager, packages = m, [str(p) for p in spec[m]]
                    break
        elif isinstance(spec, (list, tuple)):
            manager, packages = mgrs[0], [str(p) for p in spec]
        if not manager or not packages:
            return None
        flags = _INSTALL_FLAGS.get(manager, ["install"])
        argv = [manager, *flags, *packages]
        privileged = manager in _SUDO_MANAGERS
        if privileged and shutil.which("sudo"):
            argv = ["sudo", *argv]
        return " ".join(argv), argv, privileged

    def _git_command(self, step: Step, root: Path) -> tuple[str, list[str]]:
        repo = str(step.git.get("repo", ""))
        dest = root / str(step.git.get("dest", "src"))
        rev = str(step.git.get("rev", ""))
        depth = step.git.get("depth")
        if dest.exists() and (dest / ".git").exists():
            argv = ["git", "-C", str(dest), "pull", "--ff-only"]
            return " ".join(argv), argv
        argv = ["git", "clone"]
        if depth:
            argv += ["--depth", str(depth)]
        if rev:
            argv += ["--branch", rev]
        argv += [repo, str(dest)]
        return " ".join(argv), argv

    def _shell_script(self, step: Step) -> str:
        shell = step.shell
        if isinstance(shell, str):
            return shell
        if isinstance(shell, dict):
            return str(shell.get(self.platform.os) or shell.get("default") or "")
        return ""

    def _preview(self, step: Step, root: Path) -> str:
        """Best-effort display string for a step's command (never executes)."""
        try:
            if step.action == "pip":
                return self._pip_command(step)[0]
            if step.action == "system":
                built = self._system_command(step)
                return built[0] if built else ""
            if step.action == "git":
                return self._git_command(step, root)[0]
            if step.action == "shell":
                return self._shell_script(step)
        except Exception:  # pragma: no cover - preview is best-effort only
            return ""
        return ""

    # -- execution --------------------------------------------------------
    def _execute(self, step: Step, argv: list[str] | None, display: str, root: Path,
                 dry_run: bool, *, privileged: bool, shell: bool = False) -> StepResult:
        self.out(f"→ {step.label()}")
        if dry_run:
            self.out(f"  [dry-run] $ {display}")
            return StepResult(step, "dry-run", "not executed", display)
        root.mkdir(parents=True, exist_ok=True)
        rc, tail = self._run(argv, display, cwd=root, shell=shell)
        if rc == 0:
            return StepResult(step, "ran", "ok", display)
        detail = (tail or f"exit {rc}").strip().splitlines()[-1:] or [f"exit {rc}"]
        msg = detail[0]
        status = "optional-failed" if step.optional else "failed"
        return StepResult(step, status, msg, display)

    def _run(self, argv: list[str] | None, display: str, *, cwd: Path,
             shell: bool) -> tuple[int, str]:
        """Run a command, streaming combined output; return (rc, tail)."""
        try:
            if shell:
                proc = subprocess.Popen(display, shell=True, cwd=str(cwd),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True)
            else:
                proc = subprocess.Popen(argv or [], cwd=str(cwd),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, f"could not start: {exc}"
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            self.out(f"  {line}")
            tail.append(line)
            if len(tail) > 20:
                tail.pop(0)
        proc.wait()
        return proc.returncode or 0, "\n".join(tail)

    def _exec_probe(self, probe: Probe) -> tuple[int, str]:
        try:
            if probe.shell:
                cp = subprocess.run(probe.shell, shell=True, capture_output=True, text=True,
                                    timeout=15)
            elif probe.argv:
                cp = subprocess.run(probe.argv, capture_output=True, text=True, timeout=15)
            else:
                return 127, "empty command"
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, str(exc)
        return cp.returncode, (cp.stderr or cp.stdout or "").strip()

    def _probe_daemon(self, address: str) -> tuple[bool, str]:
        """Best-effort JSON-RPC ``hello`` against a capture daemon.

        Mirrors the client's framing tolerance (docs/logic-daemon-protocol.md):
        a daemon may emit a non-JSON banner or unsolicited notifications, so skip
        any line that is not JSON or whose ``id`` does not match our request and
        keep reading until the ``hello`` reply arrives (bounded by a deadline).
        """
        host, port = _split_hostport(address)
        if not host:
            return False, f"bad address '{address}'"
        try:
            with socket.create_connection((host, port), timeout=2.0) as sock:
                req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hello"}) + "\n"
                sock.sendall(req.encode())
                buf = b""
                deadline = time.monotonic() + 2.0
                while True:
                    while b"\n" not in buf:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False, f"daemon at {host}:{port} did not answer 'hello'"
                        sock.settimeout(remaining)
                        chunk = sock.recv(4096)
                        if not chunk:
                            return False, f"daemon at {host}:{port} closed before 'hello'"
                        buf += chunk
                    line, _, buf = buf.partition(b"\n")
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        continue                       # banner / non-JSON greeting
                    if not isinstance(msg, dict) or msg.get("id") != 1:
                        continue                       # notification / other request
                    if msg.get("error"):
                        err = msg["error"]
                        emsg = err.get("message") if isinstance(err, dict) else str(err)
                        return False, f"daemon 'hello' failed: {emsg}"
                    if "result" in msg:
                        dev = (msg["result"] or {}).get("device", {})
                        return True, dev.get("model", "daemon reachable")
                    return False, "unexpected hello reply"
        except (OSError, ValueError) as exc:
            return False, f"daemon not reachable at {host}:{port} ({exc})"

    def _resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else (self.work_root or Path.cwd()) / p


def _split_hostport(address: str) -> tuple[str, int]:
    s = address.split("://", 1)[-1].strip().rstrip("/")
    if ":" in s:
        host, _, port = s.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return "", 0
    return s, 5555


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
