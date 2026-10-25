"""Run reports: the structured result of executing a bench procedure.

A :class:`RunReport` records every step (pass/fail/skip, response, timing), the
measurements captured, and an overall verdict. It serialises to JSON (agent
mode), renders as human text (CLI default), or as Markdown (drop into a PR /
logbook). The CLI exit code mirrors :attr:`RunReport.ok`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _artifact_summary(a: dict[str, Any]) -> tuple[str, str]:
    """A one-line ``(detail, path)`` summary for any artifact kind.

    Covers the logic-analyzer kinds (``logic``/``decoded``) and the debug/flash
    kinds (``firmware``/``flash``/``erase``/``rtt``/``gdb``) so both the human and
    Markdown reports render a hardware-in-the-loop run.
    """
    kind = a.get("kind", "")
    path = str(a.get("path", "") or "")
    if kind == "decoded":
        return f"{a.get('frames', 0)} frames", path
    if kind == "logic":
        detail = f"{a.get('sample_count', 0)} samples"
        if a.get("sample_rate_hz"):
            detail += f" @{a.get('sample_rate_hz')} Hz"
        if a.get("channels"):
            detail += f" ch={a.get('channels')}"
        return detail, path
    if kind == "firmware":
        detail = f"sha256={str(a.get('sha256', ''))[:12]} size={a.get('size_bytes', 0)}"
        if a.get("version"):
            detail += f" v={a.get('version')}"
        return detail, path
    if kind == "flash":
        fw = str(a.get("firmware", "") or "")
        name = Path(fw).name if fw else ""
        detail = (f"{a.get('tool', '')} {name} sha256={str(a.get('sha256', ''))[:12]} "
                  f"verified={a.get('verified')}").strip()
        return detail, path or fw
    if kind == "erase":
        return f"{a.get('tool', '')} mass={a.get('mass')} ok={a.get('ok')}", path
    if kind == "rtt":
        n = a.get("line_count")
        if n is None:
            lines = a.get("lines")
            n = len(lines) if isinstance(lines, list) else 0
        return f"{n or 0} lines", path
    if kind == "gdb":
        return f"exit={a.get('exit_code')}", path or str(a.get("script", ""))
    return f"{a.get('sample_count', 0)} samples", path


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
    captures: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        skipped = sum(1 for s in self.steps if s.skipped)
        failed = sum(1 for s in self.steps if not s.ok and not s.skipped)
        return {
            "total": len(self.steps),
            "passed": len(self.steps) - failed - skipped,
            "failed": failed,
            "skipped": skipped,
            "measurements": len(self.measurements),
            "artifacts": len(self.artifacts),
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
        if self.captures:
            d["captures"] = self.captures
        if self.artifacts:
            d["artifacts"] = self.artifacts
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
        for c in self.captures:
            lines.append(
                f"  capture {c.get('channel', '')}: mean={c.get('mean')} {c.get('units', '')}"
                f" (n={c.get('count', 0)}, {c.get('rate_hz', 0)} Hz)"
                + (f" -> {c['file']}" if c.get("file") else "")
            )
        for a in self.artifacts:
            detail, path = _artifact_summary(a)
            lines.append(f"  artifact {a.get('kind', '')}: {detail}")
            if path:
                lines.append(f"        -> {path}")
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
        if self.captures:
            out += [
                "",
                "**Captures:**",
                "",
                "| channel | n | mean | min | max | rate (Hz) | units |",
                "|---|--:|--:|--:|--:|--:|---|",
            ]
            for c in self.captures:
                out.append(
                    f"| `{c.get('channel', '')}` | {c.get('count', 0)} | {c.get('mean')} "
                    f"| {c.get('min')} | {c.get('max')} | {c.get('rate_hz', 0)} | {c.get('units', '')} |"
                )
        if self.artifacts:
            out += [
                "",
                "**Artifacts:**",
                "",
                "| kind | detail | path |",
                "|---|---|---|",
            ]
            for a in self.artifacts:
                detail, path = _artifact_summary(a)
                out.append(
                    f"| `{a.get('kind', '')}` | {detail.replace('|', '\\|')} | `{path}` |"
                )
        return "\n".join(out)
