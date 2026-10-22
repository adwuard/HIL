"""Decoders for the Framer page payload the reference site is published with.

The reference site is a Framer site. The interesting data is *not* in the
rendered HTML tree -- it is embedded verbatim in two ``<script>`` blocks:

* ``<script type="application/ld+json">`` -- clean schema.org metadata
  (``TechArticle`` / ``Dataset`` / ``BreadcrumbList``).
* ``<script type="framer/handover" id="__framer__handoverData">`` -- a
  *flat* JSON "heap": a list where dicts/lists reference other entries by
  integer index. The full per-device SCPI command set lives here as one big
  JSON string, alongside the vendor/name/description/count scalars.

This module only knows how to get raw structures out of those two blocks;
domain meaning (what a "command" is) lives in :mod:`hil.scraper.reference`.
"""

from __future__ import annotations

import json
import re
from typing import Any

HANDOVER_RE = re.compile(
    r'<script[^>]*type="framer/handover"[^>]*>(.*?)</script>', re.S
)
LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


class FramerError(ValueError):
    """A Framer payload could not be located or decoded."""


def extract_handover(html: str) -> list[Any]:
    """Return the decoded ``__framer__handoverData`` flat heap.

    The heap is a JSON list; entries reference each other by index. Use
    :class:`FramerHeap` to resolve references into real Python objects.
    """
    match = HANDOVER_RE.search(html)
    if match is None:
        raise FramerError(
            "no <script type=\"framer/handover\"> block found -- is this a "
            "Framer page, or did the layout change?"
        )
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed page
        raise FramerError(f"handover block is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise FramerError("handover block is not a JSON list as expected.")
    return data


def extract_ldjson(html: str) -> dict[str, Any] | None:
    """Return the first ``application/ld+json`` object, or ``None``."""
    match = LDJSON_RE.search(html)
    if match is None:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


class FramerHeap:
    """Resolve a Framer flat heap (index-referenced list) into objects.

    Framer serialises a page's data as a flat list. Composite entries point at
    their children by integer index:

    * scalar (``str``/``int``/``float``/``bool``/``None``) -- itself
    * ``{"type": t, "value": v}`` -- a *typed value* wrapper; resolves to ``v``
    * ``{key: idx, ...}`` -- an object; each value is an index
    * ``["Map", k0, v0, k1, v1, ...]`` -- a JS ``Map`` -> ``dict``
    * ``[idx, ...]`` -- a plain array

    Resolution is memoised per index (the heap is a shared-value DAG) with a
    recursion stack to break cycles.
    """

    def __init__(self, data: list[Any]) -> None:
        self._data = data
        self._memo: dict[int, Any] = {}

    def __len__(self) -> int:
        return len(self._data)

    @property
    def raw(self) -> list[Any]:
        return self._data

    def scalars(self) -> list[Any]:
        """Top-level scalar entries, in order (no reference chasing).

        Device pages keep the command JSON string, the ld+json string and the
        name/vendor/date/count values as *direct* scalar elements, so a lot of
        extraction never needs full resolution.
        """
        return [x for x in self._data if isinstance(x, (str, int, float, bool))]

    def strings(self) -> list[str]:
        return [x for x in self._data if isinstance(x, str)]

    def resolve(self, index: int = 0) -> Any:
        """Resolve heap entry ``index`` (default: the root at ``0``)."""
        return self._resolve(index, frozenset())

    def _resolve(self, i: int, stack: frozenset[int]) -> Any:
        if not isinstance(i, int) or i < 0 or i >= len(self._data):
            return None
        key = i
        if key in self._memo:
            return self._memo[key]
        if i in stack:  # cycle guard
            return None
        stack = stack | {i}
        value = self._data[i]
        result = self._resolve_value(value, stack)
        self._memo[key] = result
        return result

    def _resolve_value(self, value: Any, stack: frozenset[int]) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            if set(value) == {"type", "value"}:
                return self._ref(value["value"], stack)
            return {k: self._ref(v, stack) for k, v in value.items()}
        if isinstance(value, list):
            if value and value[0] == "Map":
                rest = value[1:]
                out: dict[str, Any] = {}
                for j in range(0, len(rest) - 1, 2):
                    out[str(self._ref(rest[j], stack))] = self._ref(rest[j + 1], stack)
                return out
            return [self._ref(x, stack) for x in value]
        return value

    def _ref(self, x: Any, stack: frozenset[int]) -> Any:
        """A child slot: ints are references, anything else is a literal."""
        if isinstance(x, int) and not isinstance(x, bool):
            return self._resolve(x, stack)
        return x


def walk_records(node: Any, pred) -> list[Any]:
    """Depth-first collect every sub-object satisfying ``pred(obj)``."""
    found: list[Any] = []
    seen: set[int] = set()

    def rec(obj: Any) -> None:
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, dict):
            if pred(obj):
                found.append(obj)
            for v in obj.values():
                rec(v)
        elif isinstance(obj, list):
            for v in obj:
                rec(v)

    rec(node)
    return found
