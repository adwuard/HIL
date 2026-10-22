"""Run reports: the structured result of executing a bench procedure.

A :class:`RunReport` records every step (pass/fail/skip, response, timing), the
measurements captured, and an overall verdict. It serialises to JSON (agent
mode), renders as human text (CLI default), or as Markdown (drop into a PR /
logbook). The CLI exit code mirrors :attr:`RunReport.ok`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepResult:
    """Outcome of one executed step."""

    index: int
    type: str
    summary: str
    ok: bool
    skipped: bool = False
    response: str | None = None
    value: Any = None
    message: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "index": self.index,
            "type": self.type,
            "summary": self.summary,
            "ok": self.ok,
        }
        if self.skipped:
            d["skipped"] = True
        if self.response is not None:
            d["response"] = self.response
        if self.value is not None:
            d["value"] = self.value
        if self.message:
            d["message"] = self.message
        if self.errors:
            d["errors"] = self.errors
        if self.elapsed_s:
            d["elapsed_s"] = self.elapsed_s
        return d


@dataclass
class Measurement:
    """A captured measurement (from a ``measure`` step)."""

    name: str
    command: str
    value: Any
    units: str = ""
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "command": self.command, "value": self.value}
        if self.units:
            d["units"] = self.units
        if not self.ok:
            d["ok"] = False
        return d

    def label(self) -> str:
        return f"{self.name}={self.value}{(' ' + self.units) if self.units else ''}"


@dataclass
class RunReport:
    """The full result of one procedure run."""

    name: str
    ok: bool
    mode: str = "sim"            # "sim" | "live"
    target: str = "simulator"
    device: str = ""
    description: str = ""
    started_at: str = ""
    duration_s: float = 0.0
    steps: list[StepResult] = field(default_factory=list)
    measurements: list[Measurement] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        skipped = sum(1 for s in self.steps if s.skipped)
        failed = sum(1 for s in self.steps if not s.ok and not s.skipped)
        return {
            "total": len(self.steps),
            "passed": len(self.steps) - failed - skipped,
            "failed": failed,
            "skipped": skipped,
            "measurements": len(self.measurements),
        }

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": self.ok,
            "name": self.name,
            "mode": self.mode,
            "target": self.target,
            "counts": self.counts(),
            "duration_s": round(self.duration_s, 4),
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.device:
            d["device"] = self.device
        if self.description:
            d["description"] = self.description
        if self.started_at:
            d["started_at"] = self.started_at
        if self.measurements:
            d["measurements"] = [m.to_dict() for m in self.measurements]
        return d

    def human(self) -> str:
        c = self.counts()
        verdict = "PASS" if self.ok else "FAIL"
        head = f"{verdict}  {self.name}  [{self.mode}: {self.target}]"
        if self.device:
            head += f"  device={self.device}"
        lines = [head]
        lines.append(
            f"  {c['passed']}/{c['total']} steps passed"
            + (f", {c['failed']} failed" if c["failed"] else "")
            + (f", {c['skipped']} skipped" if c["skipped"] else "")
            + f"  ({self.duration_s:.3f}s)"
        )
        for s in self.steps:
            mark = "skip" if s.skipped else ("ok  " if s.ok else "FAIL")
            line = f"  [{s.index:>2}] {mark} {s.summary}"
            if s.response is not None and not s.skipped:
                line += f"  -> {s.response}"
            lines.append(line)
            if s.message and not s.ok:
                lines.append(f"        {s.message}")
        if self.measurements:
            lines.append("  measurements: " + ", ".join(m.label() for m in self.measurements))
        return "\n".join(lines)

    def markdown(self) -> str:
        c = self.counts()
        verdict = "✅ PASS" if self.ok else "❌ FAIL"
        out = [
            f"# {self.name} — {verdict}",
            "",
            f"- **mode:** `{self.mode}` → `{self.target}`"
            + (f" · **device:** `{self.device}`" if self.device else ""),
            f"- **steps:** {c['passed']}/{c['total']} passed"
            + (f", {c['failed']} failed" if c["failed"] else "")
            + (f", {c['skipped']} skipped" if c["skipped"] else "")
            + f" · **duration:** {self.duration_s:.3f}s",
        ]
        if self.description:
            out += ["", self.description]
        out += ["", "| # | step | result | detail |", "|---:|---|---|---|"]
        for s in self.steps:
            mark = "⏭ skip" if s.skipped else ("✅" if s.ok else "❌")
            detail = s.message or (s.response if s.response is not None else "")
            out.append(
                f"| {s.index} | `{s.summary}` | {mark} | {str(detail).replace('|', '\\|')} |"
            )
        if self.measurements:
            out += ["", "**Measurements:** " + ", ".join(f"`{m.label()}`" for m in self.measurements)]
        return "\n".join(out)
