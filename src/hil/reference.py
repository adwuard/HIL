"""On-demand reference corpus index — the command ground-truth behind ``hil help``.

HIL ships a curated, *lazily loaded* index over the scraped SCPI reference corpus
(725 instruments, ~294k commands). It answers the question an agent actually has
mid-session — "what is the correct syntax for this exact model?" — without ever
bulk-loading the corpus (that would push :func:`~hil.registry.load_registry` from
~9 ms to ~800 ms; see the registry-vs-corpus separation decision).

Two tiers, deliberately split:

* **catalog** (small, ~144 KB) — slug/name/category/url for every instrument. A
  copy is bundled inside the package (``hil/reference/catalog.json``) so
  model → slug matching works even on a bare installed tool with no corpus on
  disk.
* **commands** (large, ~45 MB archive) — the full per-device command set. These
  are resolved *on demand* from an external corpus root and never cached in bulk:
  exactly one ``devices/<slug>.json`` member is read per lookup.

Corpus roots are searched in this order (first hit wins per tier):

1. ``$HIL_REFERENCE_DIR``
2. the repo snapshot ``<repo>/data/reference/v1`` (found by walking up)
3. ``~/.cache/hil/reference/v1``
4. the bundled package index ``hil/reference`` (catalog only)

A root may hold either an unpacked ``devices/<slug>.json`` tree or the
``reference-corpus-v1.tar.gz`` archive; both are supported, and the archive is
read member-by-member so no extraction to disk is needed.
"""

from __future__ import annotations

import json
import os
import re
import tarfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

# Bundled catalog (always present with the package).
_BUNDLED = Path(__file__).parent
_ENV_ROOT = "HIL_REFERENCE_DIR"
_ARCHIVE_GLOBS = ("reference-corpus-*.tar.gz", "reference-corpus-*.tgz")

# Tokens that carry no discriminating power when matching a model string.
_STOPWORDS = {
    "technologies", "technology", "inc", "ltd", "gmbh", "co", "corp",
    "the", "and", "series", "instrument", "instruments",
}

# A catalog match must clear this score to be trusted. In practice real matches
# score ~1.0 (model token present) while vendor-only overlaps — e.g. asking for a
# "Keysight 34461A" and hitting "keysight-ac6801b" — land at ~0.45. Returning a
# wrong device's SCPI is worse than reporting no match, so anything below this is
# treated as "not confidently in the corpus" and surfaced with near-misses instead.
MIN_CONFIDENCE = 0.6


def _repo_snapshot() -> Path | None:
    """Find ``<repo>/data/reference/v1`` by walking up from the package and CWD."""
    for start in (Path(__file__).resolve(), Path.cwd()):
        for parent in (start, *start.parents):
            cand = parent / "data" / "reference" / "v1"
            if (cand / "catalog.json").is_file():
                return cand
            if (parent / ".git").exists():
                break
    return None


def _candidate_roots() -> list[Path]:
    """Corpus roots in priority order (deduped, existing-only)."""
    roots: list[Path] = []
    env = os.environ.get(_ENV_ROOT, "").strip()
    if env:
        roots.append(Path(env).expanduser())
    snap = _repo_snapshot()
    if snap is not None:
        roots.append(snap)
    roots.append(Path.home() / ".cache" / "hil" / "reference" / "v1")
    roots.append(_BUNDLED)  # catalog-only fallback, always last
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        rp = r.resolve() if r.exists() else r
        if rp not in seen:
            seen.add(rp)
            out.append(r)
    return out


def _norm_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _model_tokens(text: str) -> list[str]:
    """Split a model/name into meaningful alphanumeric tokens."""
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in raw if t not in _STOPWORDS and len(t) > 1]


# -- SCPI mnemonic matching --------------------------------------------------
# A SCPI mnemonic mixes short/long forms and optional brackets: ``:OUTPut[:STATe]``
# accepts ``OUTP``, ``OUTPUT``, ``OUTP:STAT`` and ``OUTPUT:STATE``. To match a
# user's query against the corpus we reduce each keyword to its LONG form (all
# letters, upper-cased) and align segment-by-segment, letting optional (bracketed)
# segments be skipped and accepting any unambiguous prefix on either side.
def _scpi_segments(mnemonic: str) -> tuple[list[tuple[str, bool]], bool]:
    """Parse a mnemonic into ``([(longform, optional), ...], is_query)``."""
    m = (mnemonic or "").strip()
    is_query = m.endswith("?")
    if is_query:
        m = m[:-1]
    m = m.lstrip(":")
    segs: list[tuple[str, bool]] = []
    cur = ""
    depth = 0

    def flush(optional: bool) -> None:
        nonlocal cur
        letters = re.sub(r"[^A-Za-z]", "", cur).upper()
        if letters:
            segs.append((letters, optional))
        cur = ""

    for ch in m:
        if ch == "[":
            flush(False)
            depth += 1
        elif ch == "]":
            flush(True)
            depth = max(0, depth - 1)
        elif ch == ":":
            flush(depth > 0)
        else:
            cur += ch
    flush(depth > 0)
    return segs, is_query


def _scpi_match(mnemonic: str, query: str) -> bool:
    """True when ``query`` (a user's SCPI keyword path) addresses ``mnemonic``.

    Alignment is attempted from every mnemonic offset, so a fragment addresses the
    command whether it is a prefix (``OUTP:STAT`` → ``:OUTPut[:STATe]``) or targets
    a deeper segment (``VOLT`` → ``:MEASure[:SCALar][:VOLTage][:DC]?``). Optional
    (bracketed) segments may be skipped and either side may be an unambiguous
    prefix of the other.
    """
    mn, _ = _scpi_segments(mnemonic)
    qs, _ = _scpi_segments(query)
    if not mn or not qs:
        return False
    return any(_scpi_align(mn, qs, start) for start in range(len(mn)))


def _scpi_align(mn: list[tuple[str, bool]], qs: list[tuple[str, bool]], start: int) -> bool:
    """Align query segments ``qs`` against mnemonic ``mn`` beginning at ``start``."""
    i = start
    for qseg, _qopt in qs:
        hit = False
        while i < len(mn):
            mseg, mopt = mn[i]
            if mseg.startswith(qseg) or qseg.startswith(mseg):
                i += 1
                hit = True
                break
            if mopt:  # an optional segment the query omitted — skip it
                i += 1
                continue
            return False  # a required segment does not match
        if not hit:
            return False
    return True


def _subsystem_match(subsystem: str, query: str) -> bool:
    a = re.sub(r"[^A-Za-z]", "", subsystem or "").upper()
    b = re.sub(r"[^A-Za-z]", "", query or "").upper()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


class ReferenceIndex:
    """Lazy index over the reference corpus. Cheap to construct; loads on use."""

    def __init__(self, roots: Iterable[Path] | None = None) -> None:
        self._roots = list(roots) if roots is not None else _candidate_roots()
        self._catalog: list[dict] | None = None
        self._catalog_root: Path | None = None
        self._device_cache: dict[str, dict] = {}
        self._tar_handles: dict[Path, tarfile.TarFile] = {}

    # -- catalog -----------------------------------------------------------
    def _find_catalog(self) -> tuple[list[dict], Path] | None:
        for root in self._roots:
            path = root / "catalog.json"
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                devices = data.get("devices") if isinstance(data, dict) else None
                if isinstance(devices, list):
                    return devices, root
        return None

    @property
    def catalog(self) -> list[dict]:
        """Every catalog entry ``{slug, name, category, url}`` (loaded once)."""
        if self._catalog is None:
            found = self._find_catalog()
            if found is None:
                self._catalog, self._catalog_root = [], None
            else:
                self._catalog, self._catalog_root = found
        return self._catalog

    @property
    def catalog_root(self) -> Path | None:
        _ = self.catalog
        return self._catalog_root

    def available(self) -> bool:
        """True when the small catalog index is reachable (matching works)."""
        return bool(self.catalog)

    def commands_available(self) -> bool:
        """True when full per-device command files are reachable on this host."""
        return self._commands_root() is not None

    # -- command-file resolution ------------------------------------------
    def _commands_root(self) -> Path | None:
        """A root that actually holds per-device command data (dir or archive)."""
        for root in self._roots:
            if (root / "devices").is_dir():
                return root
            if self._archive_in(root) is not None:
                return root
        return None

    @staticmethod
    def _archive_in(root: Path) -> Path | None:
        if not root.is_dir():
            return None
        for pattern in _ARCHIVE_GLOBS:
            hits = sorted(root.glob(pattern))
            if hits:
                return hits[0]
        return None

    def _open_tar(self, archive: Path) -> tarfile.TarFile | None:
        handle = self._tar_handles.get(archive)
        if handle is not None:
            return handle
        try:
            handle = tarfile.open(archive, "r:gz")
        except (OSError, tarfile.TarError):
            return None
        self._tar_handles[archive] = handle
        return handle

    def _read_device_json(self, slug: str) -> dict | None:
        """Load exactly one device's command file — from an unpacked dir or the
        archive (single member read; never extracts or bulk-loads)."""
        if slug in self._device_cache:
            return self._device_cache[slug]
        for root in self._roots:
            direct = root / "devices" / f"{slug}.json"
            if direct.is_file():
                try:
                    data = json.loads(direct.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict):
                    self._device_cache[slug] = data
                    return data
            archive = self._archive_in(root)
            if archive is not None:
                tf = self._open_tar(archive)
                if tf is None:
                    continue
                for member_name in (f"devices/{slug}.json", f"./devices/{slug}.json"):
                    try:
                        member = tf.getmember(member_name)
                    except KeyError:
                        continue
                    try:
                        fh = tf.extractfile(member)
                        if fh is None:
                            continue
                        data = json.loads(fh.read().decode("utf-8"))
                    except (OSError, tarfile.TarError, json.JSONDecodeError):
                        continue
                    if isinstance(data, dict):
                        self._device_cache[slug] = data
                        return data
        return None

    # -- matching ----------------------------------------------------------
    def match(self, query: str, limit: int = 8) -> list[dict]:
        """Rank catalog entries against a model/name/IDN/slug fragment.

        Returns ``[{slug, name, category, score, exact}]`` best-first. ``query``
        may be a bare slug, a model (``DP932E``), a vendor+model
        (``Rigol DP932E``) or a full ``*IDN?`` string — it is reduced to its
        discriminating alphanumeric tokens and scored by overlap.
        """
        q = (query or "").strip()
        if not q:
            return []
        catalog = self.catalog
        # Fast path: exact slug.
        for entry in catalog:
            if entry.get("slug") == q.lower():
                return [{**_slim(entry), "score": 1.0, "exact": True}]

        # Reduce an IDN string ("Mfr,Model,Serial,FW") to model-ish tokens.
        parts = [p.strip() for p in q.split(",") if p.strip()]
        probe = parts[1] if len(parts) >= 2 else q
        probe = f"{parts[0]} {probe}" if len(parts) >= 2 else probe
        q_tokens = _model_tokens(probe) or _model_tokens(q)
        q_joined = _norm_token(probe) or _norm_token(q)
        if not q_tokens and not q_joined:
            return []

        scored: list[dict] = []
        for entry in catalog:
            name = str(entry.get("name", ""))
            slug = str(entry.get("slug", ""))
            hay_tokens = set(_model_tokens(name)) | set(_model_tokens(slug.replace("-", " ")))
            hay_joined = _norm_token(name)
            score = 0.0
            if q_joined and q_joined in hay_joined:
                score += 0.6
            if q_tokens:
                overlap = sum(1 for t in q_tokens if t in hay_tokens)
                score += 0.4 * (overlap / len(q_tokens))
            # A model token that is a substring of the slug is a strong signal.
            for t in q_tokens:
                if len(t) >= 3 and t in slug:
                    score += 0.25
                    break
            if score > 0:
                scored.append({**_slim(entry), "score": round(min(score, 1.0), 3),
                               "exact": score >= 0.99})
        scored.sort(key=lambda d: (-d["score"], d["slug"]))
        return scored[: max(1, limit)]

    def best_match(self, query: str) -> dict | None:
        hits = self.match(query, limit=1)
        return hits[0] if hits else None

    # -- full device -------------------------------------------------------
    def device(self, slug: str) -> dict | None:
        """The full reference record for ``slug`` (commands included), or None
        when the command corpus is not reachable on this host."""
        return self._read_device_json(slug)

    def subsystems(self, slug: str) -> list[str]:
        dev = self.device(slug)
        if not dev:
            return []
        subs = dev.get("subsystems")
        if isinstance(subs, list) and subs:
            return [str(s) for s in subs]
        return sorted({str(c.get("subsystem", "")) for c in dev.get("commands", [])
                       if c.get("subsystem")})

    def close(self) -> None:
        for handle in self._tar_handles.values():
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._tar_handles.clear()

    def __enter__(self) -> "ReferenceIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _slim(entry: dict) -> dict:
    return {
        "slug": entry.get("slug", ""),
        "name": entry.get("name", ""),
        "category": entry.get("category", ""),
    }


@lru_cache(maxsize=1)
def default_index() -> ReferenceIndex:
    """Process-wide default index (roots resolved once)."""
    return ReferenceIndex()


def find_commands(
    slug: str,
    command: str = "",
    subsystem: str = "",
    limit: int = 40,
) -> list[dict]:
    """Filter one device's command set by a mnemonic substring and/or subsystem.

    ``command`` is matched case-insensitively against the mnemonic with SCPI
    optional-bracket noise removed, so ``OUTP:STAT`` matches ``:OUTPut[:STATe]``.
    """
    dev = default_index().device(slug)
    if not dev:
        return []
    cmds = dev.get("commands", [])
    sub = (subsystem or "").strip()
    out: list[dict] = []
    for c in cmds:
        if not isinstance(c, dict):
            continue
        if sub and not _subsystem_match(str(c.get("subsystem", "")), sub):
            continue
        if command and not _scpi_match(str(c.get("mnemonic", "")), command):
            continue
        out.append(c)
        if len(out) >= max(1, limit):
            break
    return out


def _slim_command(c: dict) -> dict:
    """Project a corpus command to the fields an agent needs to act on it."""
    params = []
    for p in c.get("parameters", []) or []:
        if not isinstance(p, dict):
            continue
        spec = {
            "name": p.get("name", ""),
            "type": p.get("type", ""),
        }
        for key in ("range_min", "range_max", "default", "unit"):
            if p.get(key) is not None:
                spec[key] = p.get(key)
        if p.get("enum_values"):
            spec["enum"] = list(p["enum_values"])[:24]
        if p.get("optional"):
            spec["optional"] = True
        if p.get("description"):
            spec["description"] = str(p["description"])[:200]
        params.append(spec)
    out = {
        "mnemonic": c.get("mnemonic", ""),
        "query": bool(c.get("is_query", False)),
        "subsystem": c.get("subsystem", ""),
        "description": c.get("description", ""),
    }
    if params:
        out["parameters"] = params
    ret = c.get("returns")
    if isinstance(ret, dict) and ret:
        out["returns"] = {
            k: ret[k]
            for k in ("type", "unit", "description", "enum_values")
            if ret.get(k)
        }
    if c.get("examples"):
        out["examples"] = list(c["examples"])[:4]
    if c.get("section"):
        out["section"] = c.get("section")
    return out


def _confident(match: dict) -> bool:
    """True when a catalog match is trustworthy (exact slug/model, or a score that
    implies the model token — not just the vendor — matched)."""
    if not match:
        return False
    if match.get("exact"):
        return True
    try:
        return float(match.get("score", 0.0)) >= MIN_CONFIDENCE
    except (TypeError, ValueError):
        return False


def help_lookup(
    model_or_idn: str,
    subsystem: str = "",
    command: str = "",
    limit: int = 40,
) -> dict:
    """Resolve a model / ``*IDN?`` string to its reference command set.

    This is the shared engine behind the ``hil help`` CLI command and the
    ``hil_help`` MCP tool. Returns a structured dict:

    * ``matched`` — the best catalog entry (slug/name/category/score) or None.
    * ``commands_available`` — whether full command data is on this host.
    * ``commands`` — the filtered, agent-ready command list.
    * ``hint`` — what to do next when nothing matched or the corpus is absent.

    It never raises: an unmatched model is a normal, reportable outcome.
    """
    idx = default_index()
    q = (model_or_idn or "").strip()
    result: dict[str, Any] = {
        "ok": True,
        "query": q,
        "matched": None,
        "commands_available": idx.commands_available(),
        "commands": [],
    }
    if not q:
        result["ok"] = False
        result["hint"] = "pass a model, slug, or *IDN? string to look up."
        return result

    best = idx.best_match(q)
    if not best or not _confident(best):
        near = [h for h in idx.match(q, limit=5) if h.get("slug")]
        result["ok"] = False
        result["matched"] = None
        result["near"] = [{"slug": h["slug"], "name": h["name"], "score": h["score"]}
                          for h in near[:3]]
        near_txt = ""
        if near:
            listed = ", ".join(f"{h['slug']} ({h['score']})" for h in near[:3])
            near_txt = (
                " Closest catalog entries (NOT confirmed — do not assume they "
                f"apply): {listed}."
            )
        result["hint"] = (
            f"no confident reference match for '{q}'. The bundled catalog covers "
            f"{len(idx.catalog)} instruments; this exact model appears to be outside "
            f"it.{near_txt} Confirm the model with hil_probe (its *IDN?), browse "
            "nearby models with `hil devices search <vendor>`, or fetch just this "
            "device's reference on demand with `hil scrape device <slug>`."
        )
        return result

    result["matched"] = best
    dev = idx.device(best["slug"])
    if dev is None:
        result["hint"] = (
            f"matched '{best['name']}' ({best['slug']}) in the catalog, but its "
            "command set is not on this host. Point $HIL_REFERENCE_DIR at an "
            "unpacked corpus (or the reference-corpus archive), or fetch just "
            f"this device with: hil scrape device {best['slug']}"
        )
        return result

    result["device"] = {
        "slug": dev.get("slug", best["slug"]),
        "name": dev.get("name", best["name"]),
        "manufacturer": dev.get("manufacturer", ""),
        "model": dev.get("model", ""),
        "series": dev.get("series", ""),
        "category": dev.get("category", best.get("category", "")),
        "manual": dev.get("manual", ""),
        "stats": dev.get("stats", {}),
        "subsystems": dev.get("subsystems", []),
    }
    cmds = find_commands(best["slug"], command=command, subsystem=subsystem, limit=limit)
    result["commands"] = [_slim_command(c) for c in cmds]
    result["count"] = len(result["commands"])
    if not cmds and (command or subsystem):
        result["hint"] = (
            "matched the device but no command fit that filter — try a broader "
            "--subsystem, or list all commands with no filter."
        )
    return result
