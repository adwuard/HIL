"""On-chip debug + flash driver — probe/flash/halt/step/memory/RTT/GDB.

Hardware-in-the-loop is only useful if the harness can put *new firmware* on the
target and then observe/control it. This driver is the flash-and-debug half of
that loop (spec §5.4.4 *Debug Testing Bench*, §5.4.5 *Build & Flash*, and the
``debug.*`` capabilities in §5.3).

Like ``ppk2`` and ``logic``, the backend is a small swappable
:class:`DebugBackend` protocol so HIL's MIT core never vendors the (variously
GPL/BSD/Apache) toolchains — it *shells out* to whichever the bench picks:

* **probe-rs** (MIT/Apache) — preferred default for Cortex-M flash/erase/reset/RTT.
* **pyOCD** (Apache) — best for register/memory access and step/halt/go.
* **OpenOCD** (GPL-2.0) — the widest target support; ``mdw``/``mww``/``reg``/``program``.
* **esptool** (GPL-2.0) — Espressif ROM/OTA flashing (``write_flash``/``erase_flash``).
* **stlink** (BSD-3) — ``st-flash``/``st-info`` for STM32.

``debug://auto?target=<chip>`` routes by chip family (``esp*``→esptool,
``stm32*``→stlink, else→probe-rs). Every tool is invoked as a subprocess, so the
GPL ones stay isolated in their own process. Real-tool memory/register parsing is
best-effort (tool output formats differ); the deterministic path for CI, ``--sim``
and unit tests is :class:`DebugSimBackend`, an in-memory Cortex-M-like target.

A curated **pseudo-SCPI facade** lets a debug probe reuse the rest of HIL
unchanged (registry safety policy, bench procedures, ``--sim``, CLI ``--json``)::

    *IDN?  :PROBE?  :TARG?  :STAT?  :FW?
    :HALT  :RESUME (:RUN)  :STEP  :RESET  :ERASE  :FLASH <firmware>
    :MEM:ADDR <hex>  :MEM:WIDTH <8|16|32>  :MEM:READ?  :MEM:WRITE <hex>[,<hex>...]
    :REG:SEL <name>  :REG:READ?  :REG:WRITE <hex>  :RTT <seconds>  :GDB <script>

The bench ``flash``/``debug`` steps and the ``hil debug`` CLI call the richer
:meth:`DebugInstrument.flash` / :meth:`DebugInstrument.halt` / … methods directly
(duck-typed), which return plain artifact dicts the report can render.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from ..addresses import Address
from ..artifacts import ArtifactSpool, fingerprint_firmware, new_id
from ..errors import HilError
from ..instrument import BaseInstrument
from ..registry import Device
from ..telemetry import Sample

_CAPTURE_ROW_CAP = 4096        # hard bound on a compatibility capture() poll loop
_DEFAULT_GDB = ("gdb-multiarch", "arm-none-eabi-gdb", "gdb")

# tool slug -> candidate binaries on PATH (first match wins)
_TOOL_BINS: dict[str, tuple[str, ...]] = {
    "probe-rs": ("probe-rs",),
    "openocd": ("openocd",),
    "esptool": ("esptool.py", "esptool"),
    "stlink": ("st-flash",),
    "pyocd": ("pyocd",),
}
# tool slug -> provisioning recipe slug (for the "run `hil setup X`" hint)
_TOOL_RECIPE = {
    "probe-rs": "probe-rs",
    "openocd": "openocd",
    "esptool": "esptool",
    "stlink": "stlink",
    "pyocd": "pyocd",
}

# The bench ``debug`` step's action verbs (``flash`` has its own step type).
DEBUG_ACTIONS = (
    "probe", "info", "halt", "resume", "run", "step", "reset", "erase",
    "read-mem", "write-mem", "read-reg", "write-reg", "gdb", "rtt",
    "wait-bootloader",
)


# -- helpers ----------------------------------------------------------------
def _as_int(value: Any) -> int:
    """Parse an int that may arrive as ``0x..``/``..``/int (YAML gives ints)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def _hex(value: Any) -> str:
    return f"0x{int(value):x}"


def _tail(res: dict | None, n: int = 400) -> str:
    """A short, single-line summary of a subprocess result for error messages."""
    if not res:
        return ""
    out = (res.get("output") or "").strip()
    if res.get("timeout"):
        out = (out + " [timed out]").strip()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:n]


def _parse_hex_words(text: str) -> list[int]:
    """Best-effort extraction of hex words from ``mdw``/``read32``/``reg`` output.

    Handles the common shapes — ``0x08000000: 20002000 08000151`` (OpenOCD),
    ``08000000:  20002000 ...  |....|`` (pyOCD, ASCII gutter dropped) and
    ``40000000 = 71`` (esptool) — by dropping a leading ``addr:`` token and
    keeping every remaining hex word.
    """
    words: list[int] = []
    for raw in (text or "").splitlines():
        line = raw.split("|", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        if toks and toks[0].endswith(":"):
            toks = toks[1:]
        for tok in toks:
            tok = tok.strip().rstrip(":").rstrip(",")
            if re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", tok):
                words.append(int(tok, 16))
    return words


_PROBE_NOISE = re.compile(
    r"no debug probes|no probes|following (?:debug )?probes|probes? (?:were|was) "
    r"found|^info\b|^warning\b|^error\b", re.IGNORECASE)

# probe-rs 0.32 enumerates each probe as ``[i]: NAME -- VID:PID[-IFACE]:SERIAL (TYPE)``, e.g.
#   [0]: DAPLink CMSIS-DAP -- 0d28:0204-3:0720..908 (CMSIS-DAP)
#   [1]: STLink V3 -- 0483:3754:0026..451 (ST-LINK)
# Lifting NAME/TYPE/SERIAL out lets the CLI (and an agent) recognize and pick a
# debugger by name or serial instead of hand-matching raw VID:PID.
_PROBE_RS_ENTRY = re.compile(
    r"^\[(\d+)\]:\s*(?P<name>.+?)\s+--\s+"
    r"(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})"
    r"(?:-(?P<iface>\d+))?:(?P<serial>\S+?)"
    r"(?:\s+\((?P<type>[^)]+)\))?\s*$")


def _parse_probe_list(text: str, tool: str) -> list[dict]:
    """Best-effort parse of a debug tool's ``probe list`` output into probes.

    Tools print a header ("The following debug probes were found:") or a
    "No debug probes were found." notice — neither is a probe, and that notice
    exits 0, so wrapping every line would fabricate a phantom probe. probe-rs
    enumerates real entries as ``[i]: NAME -- VID:PID[-IFACE]:SERIAL (TYPE)``;
    those are lifted into structured fields (name/type/vid/pid/serial) so each
    debugger is recognizable without VID:PID matching. Other tools' lines fall
    back to ``{name, index?}`` (noise stripped) so their output still round-trips.
    """
    probes: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _PROBE_NOISE.search(line):
            continue
        m = _PROBE_RS_ENTRY.match(line)
        if m:
            probe: dict[str, Any] = {"tool": tool, "index": int(m.group(1)),
                                     "name": m.group("name").strip()}
            if m.group("type"):
                probe["type"] = m.group("type").strip()
            probe["vid"] = m.group("vid")
            probe["pid"] = m.group("pid")
            if m.group("iface"):
                probe["interface"] = int(m.group("iface"))
            probe["serial"] = m.group("serial")
            probes.append(probe)
            continue
        probe = {"tool": tool, "name": line}
        g = re.match(r"^\[(\d+)\]\s*[:\-]?\s*(.*)$", line)
        if g:
            probe["index"] = int(g.group(1))
            probe["name"] = g.group(2).strip() or line
        probes.append(probe)
    return probes


def route_auto(target: str = "", chip: str = "") -> str:
    """Pick a debug tool for ``debug://auto`` from the chip family (spec §5.4.5)."""
    t = (target or chip or "").lower()
    if "esp" in t:
        return "esptool"
    if "stm32" in t:
        return "stlink"
    return "probe-rs"


# -- backend protocol -------------------------------------------------------
class DebugBackend(Protocol):
    """The minimal, swappable surface a debug/flash backend must provide.

    Methods return plain dicts / int lists so the instrument and the bench report
    stay backend-agnostic. ``flash``/``erase``/``gdb``/``rtt`` return an *artifact
    dict* carrying ``kind`` (see :mod:`hil.bench.report`).
    """

    def open(self) -> None: ...
    def info(self) -> str: ...
    def probe_list(self) -> list[dict]: ...
    def target_info(self) -> dict: ...
    def halt(self) -> dict: ...
    def resume(self) -> dict: ...
    def step(self) -> dict: ...
    def reset(self, halt: bool = False) -> dict: ...
    def read_mem(self, addr: int, width: int = 32, count: int = 1) -> list[int]: ...
    def write_mem(self, addr: int, values: list[int], width: int = 32) -> None: ...
    def read_reg(self, name: str) -> int: ...
    def write_reg(self, name: str, value: int) -> None: ...
    def flash(self, firmware: str, *, chip: str = "", address: int | None = None,
              verify: bool = True, erase: bool = False) -> dict: ...
    def erase(self, *, chip: str = "", mass: bool = True) -> dict: ...
    def gdb(self, script: str, *, gdb_bin: str = "") -> dict: ...
    def rtt(self, duration_s: float = 5.0) -> dict: ...
    def wait_bootloader(self, timeout_s: float = 10.0) -> dict: ...
    def close(self) -> None: ...


# -- simulator backend -----------------------------------------------------
class DebugSimBackend:
    """An in-memory, Cortex-M-like target so flash/debug runs need no hardware.

    Seeded with a plausible vector table (SP at ``0x0``, reset vector at ``0x4``),
    a couple of peripheral registers, a core register file, and a fixed RTT log.
    ``0x40000004`` is a free-running tick counter (increments on each read) so a
    ``capture`` poll produces a ramp rather than a flat line.
    """

    def __init__(self, spool: ArtifactSpool | None = None, *,
                 chip: str = "sim-target", fw_version: str = "sim-1.0.0",
                 core: str = "cortex-m4") -> None:
        self.spool = spool or ArtifactSpool()
        self._chip = chip
        self._fw_version = fw_version
        self._core = core
        self._tick = 0x0F
        self._mem: dict[int, int] = {
            0x00000000: 0x20002000,   # initial stack pointer
            0x00000004: 0x08000151,   # reset vector (thumb)
            0x40000000: 0x71,         # WHO_AM_I
            0x40000004: 0x0F,         # free-running tick counter
        }
        self._regs: dict[str, int] = {
            "pc": 0x08000150, "sp": 0x20002000, "lr": 0xFFFFFFFF,
            "xpsr": 0x01000000, "r0": 0, "r1": 1, "r2": 2, "r3": 3,
            "r4": 0x20000000, "r12": 0,
        }
        self._probes = [{
            "id": "SIM-PROBE-0001", "vendor": "HIL", "serial": "SIM0001",
            "name": "Simulated CMSIS-DAP", "transport": "swd",
        }]
        self._rtt = [
            f"[HIL] ready chip={chip} fw={fw_version}",
            "[HIL] event boot t=12",
            "[HIL] metric vbat_mv=3300",
            "[HIL] event wifi_connected t=1234",
        ]
        self._state = "running"
        self._flashed: dict | None = None

    def open(self) -> None:
        pass

    def info(self) -> str:
        return "debug-sim"

    def probe_list(self) -> list[dict]:
        return [dict(p) for p in self._probes]

    def target_info(self) -> dict:
        return {
            "tool": "simulator", "vendor": "HIL", "chip": self._chip,
            "target": self._chip, "core": self._core, "state": self._state,
            "fw_version": self._flashed_version() or self._fw_version,
            "probe": self._probes[0]["serial"], "transport": "swd",
            "flashed": bool(self._flashed),
        }

    def _flashed_version(self) -> str:
        return str((self._flashed or {}).get("version", "")) if self._flashed else ""

    def halt(self) -> dict:
        self._state = "halted"
        return {"state": "halted", "tool": "simulator", "pc": _hex(self._regs["pc"])}

    def resume(self) -> dict:
        self._state = "running"
        return {"state": "running", "tool": "simulator"}

    def step(self) -> dict:
        self._state = "halted"
        self._regs["pc"] = (self._regs["pc"] + 2) & 0xFFFFFFFF
        return {"state": "halted", "tool": "simulator", "pc": _hex(self._regs["pc"]),
                "stepped": True}

    def reset(self, halt: bool = False) -> dict:
        self._regs["sp"] = self._mem.get(0x0, self._regs["sp"])
        self._regs["pc"] = self._mem.get(0x4, self._regs["pc"]) & ~1
        self._state = "halted" if halt else "running"
        return {"state": self._state, "tool": "simulator", "pc": _hex(self._regs["pc"])}

    def read_mem(self, addr: int, width: int = 32, count: int = 1) -> list[int]:
        wb = max(1, int(width) // 8)
        mask = (1 << (wb * 8)) - 1
        out: list[int] = []
        for i in range(max(1, int(count))):
            a = int(addr) + i * wb
            if a == 0x40000004:            # free-running tick counter
                self._tick = (self._tick + 1) & 0xFFFFFFFF
                self._mem[a] = self._tick
            out.append(self._mem.get(a, 0) & mask)
        return out

    def write_mem(self, addr: int, values: list[int], width: int = 32) -> None:
        wb = max(1, int(width) // 8)
        mask = (1 << (wb * 8)) - 1
        for i, v in enumerate(values or []):
            self._mem[int(addr) + i * wb] = int(v) & mask

    def read_reg(self, name: str) -> int:
        return int(self._regs.get(str(name).lower(), 0)) & 0xFFFFFFFF

    def write_reg(self, name: str, value: int) -> None:
        self._regs[str(name).lower()] = int(value) & 0xFFFFFFFF

    def flash(self, firmware: str, *, chip: str = "", address: int | None = None,
              verify: bool = True, erase: bool = False) -> dict:
        fp = fingerprint_firmware(firmware, tool="simulator")
        self._flashed = {**fp, "chip": chip or self._chip,
                         "address": _hex(address) if address is not None else "0x0",
                         "verified": bool(verify), "erased": bool(erase)}
        self.reset(halt=False)
        return {"kind": "flash", "id": new_id("flash"), "tool": "simulator",
                "firmware": str(firmware), "sha256": fp["sha256"],
                "size_bytes": fp["size_bytes"], "chip": self._flashed["chip"],
                "address": self._flashed["address"], "verified": bool(verify),
                "erased": bool(erase), "duration_s": 0.0, "ok": True,
                "detail": "simulated flash + verify"}

    def erase(self, *, chip: str = "", mass: bool = True) -> dict:
        self._flashed = None
        for a in [a for a in self._mem if a >= 0x08000000]:
            del self._mem[a]
        return {"kind": "erase", "id": new_id("erase"), "tool": "simulator",
                "chip": chip or self._chip, "mass": bool(mass), "ok": True,
                "detail": "simulated mass erase"}

    def gdb(self, script: str, *, gdb_bin: str = "") -> dict:
        out = (f"[sim] gdb --batch -x {script}\n"
               "Breakpoint 1, main () at src/main.c:42\n"
               f"42\t    app_init();\n"
               f"[sim] target halted pc={_hex(self._regs['pc'])}\n")
        return {"kind": "gdb", "id": new_id("gdb"), "tool": "simulator",
                "script": str(script), "gdb": gdb_bin or "gdb-sim",
                "exit_code": 0, "output": out}

    def rtt(self, duration_s: float = 5.0) -> dict:
        lines = list(self._rtt)
        return {"kind": "rtt", "id": new_id("rtt"), "tool": "simulator",
                "lines": lines, "output": "\n".join(lines),
                "duration_s": float(duration_s)}

    def wait_bootloader(self, timeout_s: float = 10.0) -> dict:
        self._state = "bootloader"
        return {"ok": True, "state": "bootloader", "tool": "simulator",
                "detail": "simulated bootloader detected (BOOT held)"}

    def close(self) -> None:
        self._state = "running"


# -- real tool backend (subprocess) ----------------------------------------
class ToolBackend:
    """Drives an external flash/debug tool as a subprocess.

    The argv builders live in the pure :meth:`_command` method (no PATH or I/O),
    so they are unit-testable without any tool installed. :meth:`open` resolves the
    binary and raises an actionable ``hil setup`` hint if it is missing.
    """

    def __init__(self, tool: str, *, target: str = "", chip: str = "", probe: str = "",
                 transport: str = "swd", interface: str = "", speed: Any = None,
                 gdb_bin: str = "", timeout: float = 30.0) -> None:
        self.tool = (tool or "probe-rs").lower()
        if self.tool not in _TOOL_BINS:
            raise HilError(
                f"debug driver: unknown tool '{tool}'. Supported: "
                + ", ".join(sorted(_TOOL_BINS)) + ", or 'auto'/'simulator'."
            )
        self.target = target or chip or ""
        self.chip = chip or target or ""
        self.probe = probe or ""
        self.transport = (transport or "swd").lower()
        self.interface = interface or ("stlink" if self.tool == "stlink" else "cmsis-dap")
        self.speed = speed
        self.timeout = timeout
        self._bin = _TOOL_BINS[self.tool][0]       # resolved to a real path in open()
        self._stinfo = "st-info"
        self._gdb_bin = gdb_bin or ""

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        for cand in _TOOL_BINS[self.tool]:
            found = shutil.which(cand)
            if found:
                self._bin = found
                break
        else:
            raise self._missing_tool_error()
        if self.tool == "stlink":
            si = shutil.which("st-info")
            if si:
                self._stinfo = si

    def close(self) -> None:
        pass

    def info(self) -> str:
        return f"{self.tool}@{self._bin}"

    def _missing_tool_error(self) -> HilError:
        recipe = _TOOL_RECIPE.get(self.tool, self.tool)
        cands = "/".join(_TOOL_BINS[self.tool])
        return HilError(
            f"debug driver: the '{self.tool}' tool ({cands}) is not installed or not "
            f"on PATH. Install it with `hil setup {recipe}` (or run `hil doctor` to "
            "see what's provisioned)."
        )

    def _unsupported(self, action: str) -> None:
        raise HilError(
            f"debug driver: the '{self.tool}' backend does not support '{action}'. "
            "Use debug://pyocd or debug://openocd for register/memory/step access, "
            "or debug://probe-rs for flash/erase/reset/rtt."
        )

    # -- pure argv builders (unit-testable; no I/O) -----------------------
    def _command(self, action: str, **kw: Any) -> list[str] | None:
        chip = kw.get("chip") or self.chip
        target = kw.get("target") or self.target or chip
        b = self._bin
        tool = self.tool

        if tool == "probe-rs":
            common = ["--chip", chip] if chip else []
            # probe-rs defaults an ST-Link/CMSIS-DAP probe to JTAG; nRF52 and most
            # Cortex-M parts are wired for SWD, so forward the URL transport
            # (debug://...?transport=swd|jtag) or the DP IDCODE read fails with
            # JtagGetIdcodeError. --speed is passed through when given.
            if self.transport:
                common += ["--protocol", self.transport]
            if kw.get("probe") or self.probe:
                common += ["--probe", str(kw.get("probe") or self.probe)]
            if self.speed:
                common += ["--speed", str(self.speed)]
            if action == "probe_list":
                return [b, "list"]
            if action == "flash":
                return [b, "download", *common, str(kw.get("firmware"))]
            if action == "erase":
                return [b, "erase", *common]
            if action in ("reset", "chip_id"):
                return [b, "reset", *common]
            if action in ("rtt", "attach"):
                return [b, "attach", *common]
            return None

        if tool == "openocd":
            cfg = ["-f", f"interface/{self.interface}.cfg"]
            if target:
                cfg += ["-f", f"target/{target}.cfg"]

            def c(cmdstr: str) -> list[str]:
                return [b, *cfg, "-c", cmdstr]

            if action == "flash":
                parts = ["program", str(kw.get("firmware"))]
                if kw.get("verify", True):
                    parts.append("verify")
                parts += ["reset", "exit"]
                return c(" ".join(parts))
            if action == "erase":
                return c("init; halt; flash erase_sector 0 0 last; resume; exit")
            if action in ("reset", "chip_id"):
                return c("init; reset run; exit")
            if action == "halt":
                return c("init; halt; exit")
            if action == "resume":
                return c("init; resume; exit")
            if action == "step":
                return c("init; halt; step; exit")
            if action == "read_mem":
                name = {8: "mdb", 16: "mdh", 32: "mdw"}.get(int(kw.get("width", 32)), "mdw")
                return c(f"init; halt; {name} {kw['addr']} {int(kw.get('count', 1))}; resume; exit")
            if action == "write_mem":
                name = {8: "mwb", 16: "mwh", 32: "mww"}.get(int(kw.get("width", 32)), "mww")
                return c(f"init; halt; {name} {kw['addr']} {kw['value']}; resume; exit")
            if action == "read_reg":
                return c(f"init; halt; reg {kw['name']}; exit")
            if action == "write_reg":
                return c(f"init; halt; reg {kw['name']} {kw['value']}; resume; exit")
            return None

        if tool == "esptool":
            pre: list[str] = []
            if self.probe:
                pre += ["--port", self.probe]
            if chip:
                pre += ["--chip", chip]
            if action in ("probe_list", "chip_id"):
                return [b, *pre, "chip_id"]
            if action == "flash":
                addr = kw.get("address")
                addr = _hex(addr) if addr is not None else "0x0"
                return [b, *pre, "write_flash", str(addr), str(kw.get("firmware"))]
            if action == "erase":
                return [b, *pre, "erase_flash"]
            if action == "reset":
                return [b, *pre, "run"]
            if action == "read_mem":
                return [b, *pre, "read_mem", str(kw["addr"])]
            if action == "write_mem":
                return [b, *pre, "write_mem", str(kw["addr"]), str(kw["value"])]
            return None

        if tool == "stlink":
            if action == "probe_list":
                return [self._stinfo, "--probe"]
            if action == "flash":
                addr = kw.get("address")
                addr = _hex(addr) if addr is not None else "0x8000000"
                return [b, "write", str(kw.get("firmware")), str(addr)]
            if action == "erase":
                return [b, "erase"]
            if action in ("reset", "chip_id"):
                return [b, "reset"]
            return None

        if tool == "pyocd":
            tf = ["-t", target] if target else []

            def cmd(cstr: str) -> list[str]:
                return [b, "cmd", *tf, "-c", cstr]

            if action == "probe_list":
                return [b, "list"]
            if action == "flash":
                return [b, "flash", *tf, str(kw.get("firmware"))]
            if action == "erase":
                return [b, "erase", *tf, "--mass"]
            if action in ("reset", "chip_id"):
                return [b, "reset", *tf]
            if action == "halt":
                return cmd("halt")
            if action == "resume":
                return cmd("go")
            if action == "step":
                return cmd("step")
            if action == "read_mem":
                return cmd(f"read32 {kw['addr']} {int(kw.get('count', 1))}")
            if action == "write_mem":
                return cmd(f"write32 {kw['addr']} {kw['value']}")
            if action == "read_reg":
                return cmd(f"reg {kw['name']}")
            if action == "write_reg":
                return cmd(f"reg {kw['name']} {kw['value']}")
            return None

        return None

    # -- subprocess runner ------------------------------------------------
    def _run(self, argv: list[str], *, timeout: float | None = None) -> dict:
        try:
            cp = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout if timeout is not None else self.timeout)
        except FileNotFoundError:
            raise self._missing_tool_error()
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return {"rc": None, "output": out, "timeout": True}
        err = cp.stderr or ""
        return {"rc": cp.returncode, "stdout": cp.stdout or "", "stderr": err,
                "output": (cp.stdout or "") + (("\n" + err) if err else ""), "timeout": False}

    def _run_ok(self, action: str, **kw: Any) -> dict:
        argv = self._command(action, **kw)
        if argv is None:
            self._unsupported(action)
        res = self._run(argv or [], timeout=kw.get("timeout"))
        if res.get("rc") not in (0, None):
            raise HilError(f"debug driver: {self.tool} '{action}' failed: {_tail(res)}")
        return res

    # -- backend surface --------------------------------------------------
    def probe_list(self) -> list[dict]:
        argv = self._command("probe_list")
        if argv is None:
            return []
        res = self._run(argv)
        if res.get("rc") not in (0, None):
            raise HilError(f"debug driver: {self.tool} probe listing failed: {_tail(res)}")
        return _parse_probe_list(res["output"], self.tool)

    def target_info(self) -> dict:
        info: dict[str, Any] = {"tool": self.tool, "chip": self.chip, "target": self.target,
                                "transport": self.transport, "probe": self.probe, "state": "unknown"}
        argv = self._command("chip_id") or self._command("probe_list")
        if argv:
            res = self._run(argv, timeout=min(self.timeout, 10.0))
            info["detail"] = _tail(res)
            info["state"] = "reachable" if res.get("rc") in (0, None) else "unreachable"
        return info

    def halt(self) -> dict:
        self._run_ok("halt")
        return {"state": "halted", "tool": self.tool}

    def resume(self) -> dict:
        self._run_ok("resume")
        return {"state": "running", "tool": self.tool}

    def step(self) -> dict:
        self._run_ok("step")
        return {"state": "halted", "tool": self.tool, "stepped": True}

    def reset(self, halt: bool = False) -> dict:
        self._run_ok("reset")
        return {"state": "halted" if halt else "running", "tool": self.tool}

    def read_mem(self, addr: int, width: int = 32, count: int = 1) -> list[int]:
        wb = max(1, int(width) // 8)
        if self.tool == "esptool":            # one word per invocation
            out: list[int] = []
            for i in range(max(1, int(count))):
                res = self._run_ok("read_mem", addr=_hex(int(addr) + i * wb))
                words = _parse_hex_words(res["output"])
                out.append(words[-1] if words else 0)
            return out
        res = self._run_ok("read_mem", addr=_hex(addr), width=width, count=count)
        words = _parse_hex_words(res["output"])
        return (words + [0] * int(count))[: int(count)]

    def write_mem(self, addr: int, values: list[int], width: int = 32) -> None:
        wb = max(1, int(width) // 8)
        for i, v in enumerate(values or []):
            self._run_ok("write_mem", addr=_hex(int(addr) + i * wb), value=_hex(v), width=width)

    def read_reg(self, name: str) -> int:
        res = self._run_ok("read_reg", name=name)
        words = _parse_hex_words(res["output"])
        if not words:
            raise HilError(f"debug driver: could not parse register '{name}' from {self.tool} output")
        return words[-1]

    def write_reg(self, name: str, value: int) -> None:
        self._run_ok("write_reg", name=name, value=_hex(value))

    def flash(self, firmware: str, *, chip: str = "", address: int | None = None,
              verify: bool = True, erase: bool = False) -> dict:
        fp = fingerprint_firmware(firmware, tool=self.tool)
        t0 = time.monotonic()
        res = self._run_ok("flash", firmware=firmware, chip=chip or self.chip,
                           address=address, verify=verify, erase=erase,
                           timeout=max(self.timeout, 120.0))
        return {"kind": "flash", "id": new_id("flash"), "tool": self.tool,
                "firmware": str(firmware), "sha256": fp["sha256"],
                "size_bytes": fp["size_bytes"], "chip": chip or self.chip,
                "address": _hex(address) if address is not None else "",
                "verified": bool(verify), "erased": bool(erase),
                "duration_s": round(time.monotonic() - t0, 3), "ok": True,
                "detail": _tail(res) or "ok"}

    def erase(self, *, chip: str = "", mass: bool = True) -> dict:
        res = self._run_ok("erase", chip=chip or self.chip, timeout=max(self.timeout, 60.0))
        return {"kind": "erase", "id": new_id("erase"), "tool": self.tool,
                "chip": chip or self.chip, "mass": bool(mass), "ok": True,
                "detail": _tail(res) or "ok"}

    def _resolve_gdb(self) -> str:
        if self._gdb_bin:
            return self._gdb_bin
        for cand in _DEFAULT_GDB:
            found = shutil.which(cand)
            if found:
                self._gdb_bin = found
                break
        return self._gdb_bin

    def gdb(self, script: str, *, gdb_bin: str = "") -> dict:
        gb = gdb_bin or self._resolve_gdb()
        if not gb:
            raise HilError(
                "debug driver: no GDB found on PATH (tried " + ", ".join(_DEFAULT_GDB) + "). "
                "Install a bare-metal GDB, e.g. `hil setup debug-gdb` or your toolchain's "
                "arm-none-eabi-gdb, then pass its script."
            )
        argv = [gb, "--batch", "-x", str(script)]
        res = self._run(argv, timeout=max(self.timeout, 60.0))
        if res.get("rc") not in (0, None):
            raise HilError(f"debug driver: gdb batch failed: {_tail(res)}")
        return {"kind": "gdb", "id": new_id("gdb"), "tool": self.tool, "script": str(script),
                "gdb": gb, "exit_code": res.get("rc"), "output": res["output"][-4000:]}

    def rtt(self, duration_s: float = 5.0) -> dict:
        argv = self._command("rtt")
        if argv is None:
            self._unsupported("rtt")
        # RTT streams forever; a timeout is the *normal* way to stop it.
        res = self._run(argv or [], timeout=max(1.0, float(duration_s)))
        text = res["output"]
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return {"kind": "rtt", "id": new_id("rtt"), "tool": self.tool, "lines": lines,
                "output": text, "duration_s": float(duration_s)}

    def wait_bootloader(self, timeout_s: float = 10.0) -> dict:
        action = "chip_id" if self.tool == "esptool" else "probe_list"
        argv = self._command(action)
        if argv is None:
            return {"ok": True, "state": "unknown", "tool": self.tool,
                    "detail": "no bootloader probe for this tool"}
        deadline = time.monotonic() + float(timeout_s)
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self._run(argv, timeout=min(5.0, float(timeout_s)))
            if last.get("rc") in (0, None):
                return {"ok": True, "state": "bootloader", "tool": self.tool,
                        "detail": _tail(last)}
            time.sleep(0.5)
        return {"ok": False, "state": "timeout", "tool": self.tool,
                "detail": _tail(last) if last else "no response"}


# -- instrument ------------------------------------------------------------
class DebugInstrument(BaseInstrument):
    """A debug probe / flasher presented through HIL's :class:`~hil.instrument.Instrument`."""

    def __init__(self, backend: DebugBackend, device: Device | None = None,
                 timeout: float = 30.0, spool: ArtifactSpool | None = None) -> None:
        super().__init__(device)
        self.timeout = timeout
        self.backend: DebugBackend = backend
        self.spool = spool or ArtifactSpool()
        self.backend.open()
        self._info: dict | None = None
        self._state = ""
        self._mem_addr = 0
        self._mem_width = 32
        self._reg = "pc"

    # -- identity ---------------------------------------------------------
    def _target(self) -> dict:
        if self._info is None:
            try:
                self._info = self.backend.target_info() or {}
            except Exception:  # pragma: no cover - tolerate an offline probe
                self._info = {"tool": getattr(self.backend, "tool", "debug")}
        return self._info

    def idn(self) -> str:
        t = self._target()
        vendor = t.get("vendor") or str(t.get("tool", "debug")).upper()
        chip = t.get("chip") or t.get("target") or "target"
        probe = t.get("probe") or ""
        fw = t.get("fw_version") or ""
        return f"{vendor},{chip},{probe},{fw}"

    # -- rich API (used by the bench flash/debug steps and `hil debug`) ---
    def probe_list(self) -> list[dict]:
        return self.backend.probe_list()

    def target_info(self) -> dict:
        self._info = None                 # force a fresh probe
        t = self._target()
        if t.get("state"):
            self._state = str(t["state"])
        return t

    def _track(self, res: dict) -> dict:
        """Remember the run/halt state from a control op so :STAT? stays live."""
        if isinstance(res, dict) and res.get("state"):
            self._state = str(res["state"])
        return res

    def halt(self) -> dict:
        self._check(":HALT")
        return self._track(self.backend.halt())

    def resume(self) -> dict:
        self._check(":RESUME")
        return self._track(self.backend.resume())

    def step(self) -> dict:
        self._check(":STEP")
        return self._track(self.backend.step())

    def reset(self, halt: bool = False) -> dict:
        self._check(":RESET")
        return self._track(self.backend.reset(halt=halt))

    def read_mem(self, addr: Any, width: int = 32, count: int = 1) -> list[int]:
        return self.backend.read_mem(_as_int(addr), int(width), int(count))

    def write_mem(self, addr: Any, values: Any, width: int = 32) -> None:
        self._check(":MEM:WRITE")
        if isinstance(values, (str, int)):
            values = [values]
        self.backend.write_mem(_as_int(addr), [_as_int(v) for v in values], int(width))

    def read_reg(self, name: str) -> int:
        return self.backend.read_reg(name)

    def write_reg(self, name: str, value: Any) -> None:
        self._check(":REG:WRITE")
        self.backend.write_reg(name, _as_int(value))

    def flash(self, firmware: str, *, chip: str = "", address: Any = None,
              verify: bool = True, erase: bool = False) -> dict:
        self._check(":FLASH")
        res = self.backend.flash(firmware, chip=chip,
                                 address=_as_int(address) if address is not None else None,
                                 verify=verify, erase=erase)
        self._info = None                 # firmware identity changed — re-probe :FW?/:TARG?
        self._state = "running"
        return res

    def erase(self, *, chip: str = "", mass: bool = True) -> dict:
        self._check(":ERASE")
        res = self.backend.erase(chip=chip, mass=mass)
        self._info = None                 # flash contents cleared — re-probe
        return res

    def gdb(self, script: str, *, out: str | None = None) -> dict:
        self._check(":GDB")
        res = self.backend.gdb(script)
        if out:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(res.get("output", ""), encoding="utf-8")
            res["path"] = str(p)
        return res

    def rtt(self, duration_s: float = 5.0, *, out: str | None = None) -> dict:
        self._check(":RTT")
        res = self.backend.rtt(duration_s)
        lines = res.get("lines") or []
        text = res.get("output") or "\n".join(lines)
        p = Path(out) if out else self.spool.new_path("rtt", "txt")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        res["path"] = str(p)
        res["line_count"] = len(lines)
        return res

    def wait_bootloader(self, timeout_s: float = 10.0) -> dict:
        return self.backend.wait_bootloader(timeout_s)

    # -- pseudo-SCPI write ------------------------------------------------
    def write(self, command: str) -> None:
        cmd = self._check(command)
        head, _, arg = cmd.partition(" ")
        head, arg = head.strip().upper(), arg.strip()
        if head == ":MEM:ADDR":
            self._mem_addr = _as_int(arg)
        elif head == ":MEM:WIDTH":
            self._mem_width = _as_int(arg)
        elif head == ":MEM:WRITE":
            vals = [_as_int(v) for v in re.split(r"[,\s]+", arg) if v.strip()]
            self.write_mem(self._mem_addr, vals, self._mem_width)
        elif head == ":REG:SEL":
            self._reg = arg.lower()
        elif head == ":REG:WRITE":
            self.write_reg(self._reg, arg)
        elif head == ":FLASH":
            self.flash(arg)
        elif head == ":ERASE":
            self.erase()
        elif head == ":HALT":
            self.halt()
        elif head in (":RESUME", ":RUN"):
            self.resume()
        elif head == ":STEP":
            self.step()
        elif head in (":RESET", "*RST"):
            self.reset()
        elif head == ":RTT":
            self.rtt(float(arg) if arg else 5.0)
        elif head == ":GDB":
            self.gdb(arg)
        else:
            raise HilError(
                f"debug driver: unsupported write '{cmd}' — see the debug device "
                "profile for the supported pseudo-SCPI set"
            )

    # -- pseudo-SCPI query ------------------------------------------------
    def query(self, command: str, timeout: float | None = None) -> str:
        up = self._check(command).upper()
        if up == "*IDN?":
            return self.idn()
        if up == ":PROBE?":
            probes = self.backend.probe_list()
            if not probes:
                return "0 probes"
            desc = "; ".join(str(p.get("name") or p.get("id") or p) for p in probes)
            return f"{len(probes)} probe(s): {desc}"
        if up in (":TARG?", ":TARGET?"):
            t = self._target()
            state = self._state or t.get("state", "")
            return (f"chip={t.get('chip', '')} core={t.get('core', '')} "
                    f"state={state} tool={t.get('tool', '')}").strip()
        if up == ":STAT?":
            return self._state or str(self._target().get("state", "unknown"))
        if up == ":FW?":
            t = self._target()
            if t.get("flashed"):
                return f"flashed {t.get('fw_version', '')}".strip()
            return str(t.get("fw_version", "") or "none")
        if up == ":MEM:READ?":
            words = self.read_mem(self._mem_addr, self._mem_width, 1)
            return _hex(words[0]) if words else "0x0"
        if up == ":REG:READ?":
            return _hex(self.read_reg(self._reg))
        if up == "*OPC?":
            return "1"
        if up == "SYST:ERR?":
            return '0,"No error"'
        raise HilError(f"debug driver: unsupported query '{command}'")

    # -- bounded compatibility capture (poll a register or memory word) ---
    def capture(self, channel: str, seconds: float, interval: float | None = None,
                units: str = "") -> list[Sample]:
        """Poll a register (``pc``/``r0``/…) or memory word (``mem:0xADDR``/``0xADDR``).

        Lets the generic bench ``capture`` step observe a target counter or
        register over time — bounded to ``_CAPTURE_ROW_CAP`` samples.
        """
        kind, ref = self._capture_target(channel)
        step = float(interval) if interval else 0.1
        end = time.monotonic() + float(seconds)
        out: list[Sample] = []
        while time.monotonic() < end and len(out) < _CAPTURE_ROW_CAP:
            ts = time.time()
            try:
                val = (float(self.backend.read_mem(ref, 32, 1)[0]) if kind == "mem"
                       else float(self.backend.read_reg(str(ref))))
            except (HilError, ValueError, IndexError):
                val = None
            if val is not None:
                out.append(Sample(ts=ts, channel=channel, value=val, units=units or "count"))
            time.sleep(step)
        return out

    def _capture_target(self, channel: str) -> tuple[str, Any]:
        s = (channel or "").strip()
        low = s.lower()
        if low.startswith("mem:"):
            return "mem", _as_int(s.split(":", 1)[1])
        if low.startswith("0x") or low.startswith("0b"):
            return "mem", _as_int(s)
        return "reg", (low or "pc")

    # -- SCPI-common lifecycle overrides ---------------------------------
    def opc(self) -> bool:
        return True

    def wait_until_done(self, timeout: float = 30.0) -> bool:
        return True

    def drain_errors(self) -> list[str]:
        return []

    def close(self) -> None:
        try:
            self.backend.close()
        except Exception:  # pragma: no cover - close must never raise
            pass


def debug_driver(addr: Address, device: Device | None, timeout: float) -> DebugInstrument:
    """Factory for ``debug://BACKEND[?target=..&probe=..&transport=swd]``.

    ``BACKEND`` is one of ``simulator``/``sim`` (in-memory target), ``auto``
    (route by chip family), or a concrete tool: ``probe-rs``/``openocd``/
    ``esptool``/``stlink``/``pyocd``.
    """
    backend = (addr.host or "").strip().lower() or "auto"
    params = dict(addr.params or {})
    target = params.get("target") or params.get("chip") or ""
    chip = params.get("chip") or params.get("target") or ""

    if backend in ("sim", "simulator"):
        return DebugInstrument(DebugSimBackend(chip=chip or "sim-target"),
                               device=device, timeout=timeout)

    tool = route_auto(target, chip) if backend == "auto" else backend
    be = ToolBackend(tool, target=target, chip=chip, probe=params.get("probe", ""),
                     transport=params.get("transport", "swd"),
                     interface=params.get("interface", ""), speed=params.get("speed"),
                     gdb_bin=params.get("gdb", ""), timeout=timeout)
    return DebugInstrument(be, device=device, timeout=timeout)
