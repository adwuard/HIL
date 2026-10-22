"""Timestamped telemetry: durable sample capture + analytics.

Every instrument can produce a stream of :class:`Sample` records ``(ts, channel,
value, units)``. A :class:`DataLogger` persists them append-only so long runs
(minutes → days) never lose data on a crash, and :func:`summarize` turns a
sample list into analytics (count, duration, rate, mean/min/max, percentiles,
std, and energy when the channel is power).

Two durable formats, both plain and diff-friendly:

* **NDJSON** (``.ndjson``/``.jsonl``) — one JSON object per line; append-safe,
  streamable, machine-readable. Default.
* **CSV** (``.csv``) — ``ts,channel,value,units``; opens in any spreadsheet.

Only the standard library is used, so telemetry has no extra dependencies.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


@dataclass
class Sample:
    """One timestamped telemetry reading."""

    ts: float                    # unix epoch seconds (float)
    channel: str                 # e.g. ":MEAS:CURR?", "current", "power"
    value: float
    units: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {"ts": self.ts, "channel": self.channel, "value": self.value}
        if self.units:
            d["units"] = self.units
        if self.meta:
            d["meta"] = self.meta
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sample":
        return cls(
            ts=float(d.get("ts", 0.0)),
            channel=str(d.get("channel", "")),
            value=float(d.get("value", 0.0)),
            units=str(d.get("units", "")),
            meta=dict(d.get("meta", {}) or {}),
        )


def _percentiles(vals: list[float]) -> dict[str, float]:
    if len(vals) < 2:
        p = vals[0] if vals else 0.0
        return {"p50": p, "p95": p, "p99": p}
    q = statistics.quantiles(vals, n=100, method="inclusive")
    return {"p50": q[49], "p95": q[94], "p99": q[98]}


def _energy(samples: list[Sample], units: str) -> dict[str, float]:
    """Trapezoidal ∫P·dt for a power channel (units == 'W')."""
    if units.strip().upper() != "W" or len(samples) < 2:
        return {}
    ordered = sorted(samples, key=lambda s: s.ts)
    joules = 0.0
    for a, b in zip(ordered, ordered[1:]):
        dt = b.ts - a.ts
        if dt > 0:
            joules += 0.5 * (a.value + b.value) * dt
    return {"energy_j": round(joules, 6), "energy_wh": round(joules / 3600.0, 6)}


def summarize(samples: Iterable[Sample], units: str = "") -> dict[str, Any]:
    """Analytics over a sample collection (empty-safe)."""
    samples = list(samples)
    vals = [s.value for s in samples if s.value is not None]
    if not vals:
        return {"count": 0, "units": units, "mean": None, "min": None, "max": None}
    ts = [s.ts for s in samples]
    duration = (max(ts) - min(ts)) if len(ts) > 1 else 0.0
    rate = ((len(vals) - 1) / duration) if duration > 0 else 0.0
    unit = units or samples[0].units
    out: dict[str, Any] = {
        "channel": samples[0].channel,
        "units": unit,
        "count": len(vals),
        "duration_s": round(duration, 4),
        "rate_hz": round(rate, 2),
        "mean": statistics.fmean(vals),
        "min": min(vals),
        "max": max(vals),
        "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        **_percentiles(vals),
        "first_ts": min(ts),
        "last_ts": max(ts),
    }
    out.update(_energy(samples, unit))
    return out


class DataLogger:
    """Append-only writer for sample streams (NDJSON or CSV, by suffix).

    Usable as a context manager; flushes after every write so a long-running
    capture survives an interrupt. ``read_samples`` loads a file back.
    """

    def __init__(self, path: str | Path, fmt: str | None = None) -> None:
        self.path = Path(path)
        self.fmt = (fmt or ("csv" if self.path.suffix.lower() == ".csv" else "ndjson")).lower()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._n = 0
        if self.fmt == "csv" and new_file:
            self._fh.write("ts,channel,value,units\n")

    def write(self, sample: Sample) -> None:
        if self.fmt == "csv":
            self._fh.write(f"{sample.ts!r},{sample.channel},{sample.value!r},{sample.units}\n")
        else:
            self._fh.write(json.dumps(sample.to_dict()) + "\n")
        self._n += 1
        self._fh.flush()

    def writemany(self, samples: Iterable[Sample]) -> int:
        for s in samples:
            self.write(s)
        return self._n

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "DataLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_samples(path: str | Path) -> list[Sample]:
    """Load samples from an NDJSON or CSV capture file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"capture file not found: {p}")
    out: list[Sample] = []
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out.append(
                    Sample(
                        ts=float(row.get("ts", 0.0) or 0.0),
                        channel=str(row.get("channel", "")),
                        value=float(row.get("value", 0.0) or 0.0),
                        units=str(row.get("units", "")),
                    )
                )
    else:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(Sample.from_dict(json.loads(line)))
    return out


def iter_channels(samples: Iterable[Sample]) -> Iterator[tuple[str, list[Sample]]]:
    """Group samples by channel, preserving first-seen order."""
    groups: dict[str, list[Sample]] = {}
    for s in samples:
        groups.setdefault(s.channel, []).append(s)
    yield from groups.items()
