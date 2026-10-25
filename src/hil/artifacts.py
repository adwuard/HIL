"""File-based capture artifacts + a short-term spool.

A logic analyzer (or any streaming capture device) produces far too much data to
hold in memory as :class:`~hil.telemetry.Sample` records: a few seconds at tens
of MSps is megabytes to gigabytes. Such devices instead return a
:class:`CaptureArtifact` — a durable file on disk (waveform ``srzip``/``csv``/
``raw``, or decoded protocol ``csv``/``json``) plus its metadata — and HIL
references it by path rather than loading it.

:class:`ArtifactSpool` is the scratch area those files land in, giving the
"short-term local storage" behaviour: entries expire by age (TTL) or beyond a
keep-count unless explicitly :meth:`retained <ArtifactSpool.retain>` (copied to a
permanent ``out`` path). Metadata is written beside each artifact as an NDJSON
sidecar (``<path>.meta.ndjson``) so a run can be inspected or re-analyzed without
reopening the (possibly huge) capture file.

Standard library only — no extra dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPOOL_ENV = "HIL_SPOOL_DIR"
DEFAULT_SPOOL = ".hil-spool"

# capture format -> file extension
_EXT = {
    "srzip": ".srzip",
    "csv": ".csv",
    "raw": ".bin",
    "bin": ".bin",
    "json": ".json",
    "ndjson": ".ndjson",
    "txt": ".txt",
    "log": ".log",
}


def _ext_for(fmt: str) -> str:
    return _EXT.get((fmt or "").strip().lower(), ".bin")


def new_id(prefix: str = "cap") -> str:
    """A short, unique, filesystem-safe artifact id."""
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def fingerprint_firmware(path: str | Path, *, version: str = "", tool: str = "") -> dict[str, Any]:
    """Hash + size a firmware image so a run records exactly what was flashed.

    Returns a ``kind="firmware"`` artifact dict (``sha256``/``size_bytes`` plus
    optional ``version``/``tool``). A missing file is *not* an error: it is
    recorded with ``size_bytes`` 0 and a hash of its name, so a simulator or
    dry-run still yields a stable fingerprint for the ``build`` -> ``flash``
    hand-off (the run bundle's firmware identity, per the reproducible-archive
    requirement).
    """
    p = Path(path)
    if p.is_file():
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
    else:
        sha = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        size = 0
    d: dict[str, Any] = {
        "kind": "firmware",
        "path": str(path),
        "sha256": sha,
        "size_bytes": size,
    }
    if version:
        d["version"] = version
    if tool:
        d["tool"] = tool
    return d


@dataclass
class CaptureArtifact:
    """A capture (or decode) result living on disk, referenced by path.

    ``kind`` is ``"logic"`` for a raw waveform capture or ``"decoded"`` for the
    output of a protocol decode (which carries ``parent`` = the source artifact
    id and ``frames`` = decoded frame count).
    """

    id: str
    kind: str                       # "logic" | "decoded"
    path: str
    format: str = ""                # srzip | csv | raw | json
    channels: list[int] = field(default_factory=list)
    sample_rate_hz: float = 0.0
    sample_count: int = 0
    duration_s: float = 0.0
    size_bytes: int = 0
    trigger: dict[str, Any] = field(default_factory=dict)
    decoders: list[Any] = field(default_factory=list)
    frames: int = 0
    parent: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
        }
        if self.format:
            d["format"] = self.format
        if self.channels:
            d["channels"] = self.channels
        if self.sample_rate_hz:
            d["sample_rate_hz"] = self.sample_rate_hz
        if self.sample_count:
            d["sample_count"] = self.sample_count
        if self.duration_s:
            d["duration_s"] = round(self.duration_s, 6)
        if self.size_bytes:
            d["bytes"] = self.size_bytes
        if self.trigger:
            d["trigger"] = self.trigger
        if self.decoders:
            d["decoders"] = self.decoders
        if self.frames:
            d["frames"] = self.frames
        if self.parent:
            d["parent"] = self.parent
        if self.meta:
            d["meta"] = self.meta
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CaptureArtifact":
        return cls(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "logic")),
            path=str(d.get("path", "")),
            format=str(d.get("format", "")),
            channels=[int(c) for c in (d.get("channels", []) or [])],
            sample_rate_hz=float(d.get("sample_rate_hz", 0.0) or 0.0),
            sample_count=int(d.get("sample_count", 0) or 0),
            duration_s=float(d.get("duration_s", 0.0) or 0.0),
            size_bytes=int(d.get("bytes", d.get("size_bytes", 0)) or 0),
            trigger=dict(d.get("trigger", {}) or {}),
            decoders=list(d.get("decoders", []) or []),
            frames=int(d.get("frames", 0) or 0),
            parent=str(d.get("parent", "")),
            meta=dict(d.get("meta", {}) or {}),
        )


def metadata_path(path: str | Path) -> Path:
    """The NDJSON sidecar path for an artifact file."""
    return Path(str(path) + ".meta.ndjson")


class ArtifactSpool:
    """A TTL-managed scratch directory for capture artifacts.

    The root defaults to ``$HIL_SPOOL_DIR`` or ``./.hil-spool``. Files created
    here are short-lived: :meth:`cleanup` drops entries older than ``max_age_s``
    (or beyond the newest ``keep_last``); :meth:`retain` copies one out to a
    permanent location so it survives cleanup.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        resolved = root or os.environ.get(SPOOL_ENV) or DEFAULT_SPOOL
        self.root = Path(resolved).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- creation ---------------------------------------------------------
    def new_path(self, kind: str, fmt: str, *, prefix: str | None = None) -> Path:
        """A fresh, unique file path in the spool (the file is not created)."""
        name = f"{prefix or kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{_ext_for(fmt)}"
        return self.root / name

    def record(self, artifact: CaptureArtifact) -> CaptureArtifact:
        """Persist an artifact's metadata sidecar and refresh its size."""
        p = Path(artifact.path)
        if artifact.path and p.exists() and not artifact.size_bytes:
            artifact.size_bytes = p.stat().st_size
        metadata_path(p).write_text(
            json.dumps(artifact.to_dict()) + "\n", encoding="utf-8"
        )
        return artifact

    # -- lookup -----------------------------------------------------------
    def read_metadata(self, path: str | Path) -> CaptureArtifact:
        """Load an artifact's metadata from its sidecar."""
        mp = metadata_path(path)
        if not mp.exists():
            raise FileNotFoundError(f"artifact metadata not found: {mp}")
        first = mp.read_text(encoding="utf-8").splitlines()[0]
        return CaptureArtifact.from_dict(json.loads(first))

    def list_artifacts(self) -> list[CaptureArtifact]:
        """All artifacts currently in the spool (newest last)."""
        out: list[CaptureArtifact] = []
        for mp in sorted(self.root.glob("*.meta.ndjson")):
            try:
                first = mp.read_text(encoding="utf-8").splitlines()[0]
                out.append(CaptureArtifact.from_dict(json.loads(first)))
            except (OSError, ValueError, IndexError):  # pragma: no cover - tolerate partials
                continue
        return out

    def resolve(self, id_or_path: str) -> Path:
        """Resolve an artifact id or path to an existing file path."""
        direct = Path(id_or_path)
        if direct.exists():
            return direct
        for art in self.list_artifacts():
            if art.id == id_or_path:
                p = Path(art.path)
                if p.exists():
                    return p
        raise FileNotFoundError(f"artifact not found in spool: {id_or_path}")

    # -- lifecycle --------------------------------------------------------
    def retain(self, id_or_path: str, dest: str | Path) -> Path:
        """Copy an artifact (and its sidecar) out of the spool to ``dest``.

        ``dest`` is treated as a **directory** (the source file name is kept)
        when it is an existing directory, ends in a path separator, or has no
        file suffix; otherwise it is a **full file path**. Returns the retained
        file's path — permanent, beyond TTL cleanup.
        """
        src = self.resolve(id_or_path)
        dest_p = Path(dest)
        as_dir = dest_p.is_dir() or str(dest).endswith(("/", os.sep)) or not dest_p.suffix
        if as_dir:
            dest_p.mkdir(parents=True, exist_ok=True)
            target = dest_p / src.name
        else:
            dest_p.parent.mkdir(parents=True, exist_ok=True)
            target = dest_p
        shutil.copy2(src, target)
        side = metadata_path(src)
        if side.exists():
            shutil.copy2(side, metadata_path(target))
        return target

    def cleanup(self, *, max_age_s: float | None = None, keep_last: int | None = None) -> list[Path]:
        """Delete expired artifacts (+ sidecars). Returns the removed files.

        With ``max_age_s``, drop anything older than that (by mtime). With
        ``keep_last``, keep the newest N and drop the rest. Both may combine;
        neither deletes anything on its own absence.
        """
        removed: list[Path] = []
        files = [p for p in self.root.iterdir() if p.is_file() and not p.name.endswith(".meta.ndjson")]
        files.sort(key=lambda p: p.stat().st_mtime)
        now = time.time()
        victims: set[Path] = set()
        if max_age_s is not None:
            victims |= {p for p in files if (now - p.stat().st_mtime) > max_age_s}
        if keep_last is not None and len(files) > keep_last:
            victims |= set(files[: len(files) - max(0, keep_last)])
        for p in victims:
            side = metadata_path(p)
            for f in (p, side):
                try:
                    if f.exists():
                        f.unlink()
                        removed.append(f)
                except OSError:  # pragma: no cover - best effort
                    continue
        return removed
