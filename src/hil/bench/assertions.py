"""Assertion operators for bench ``assert`` steps.

:func:`evaluate` never raises on a bad comparison — an assertion is *data*
(pass/fail + a human detail for the report), not a crash. Numeric coercion is
lenient so instrument replies like ``"3.301E+00"`` or ``"0..200 V"`` compare
correctly. Ranges use the registry's ``"min..max"`` style; tolerances use
``"value±tol"``.
"""

from __future__ import annotations

import re
from typing import Any

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_TOL_RE = re.compile(
    r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(?:\+/-|±|\+-)\s*"
    r"(\d*\.?\d+(?:[eE][-+]?\d+)?)"
)
_FALSY = {"", "0", "0.0", "false", "no", "off", "none", "nan"}

OPS = frozenset(
    {
        "eq", "ne", "lt", "le", "gt", "ge", "within", "approx",
        "contains", "not_contains", "matches", "not_matches",
        "exists", "is_true", "is_false",
    }
)


def _num(x: Any) -> float | None:
    """Best-effort float extraction (first number in the value)."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = _NUM_RE.search(str(x))
    return float(m.group()) if m else None


def _truthy(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return False
    s = str(x).strip().lower()
    if s in _FALSY:
        return False
    n = _num(x)
    return not (n is not None and n == 0 and _NUM_RE.fullmatch(s) is not None)


def _parse_range(expected: Any) -> tuple[float, float] | None:
    if isinstance(expected, dict):
        lo, hi = _num(expected.get("min")), _num(expected.get("max"))
    elif isinstance(expected, (list, tuple)) and len(expected) == 2:
        lo, hi = _num(expected[0]), _num(expected[1])
    else:
        s = str(expected)
        if ".." in s:  # registry-style "min..max" (units tolerated on either end)
            lo_s, _, hi_s = s.partition("..")
            lo, hi = _num(lo_s), _num(hi_s)
        else:
            nums = _NUM_RE.findall(s)
            if len(nums) < 2:
                return None
            lo, hi = float(nums[0]), float(nums[1])
    return (lo, hi) if lo is not None and hi is not None else None


def _parse_approx(expected: Any) -> tuple[float, float] | None:
    if isinstance(expected, dict):
        v = _num(expected.get("value"))
        t = _num(expected.get("tol", expected.get("tolerance")))
        return (v, t) if v is not None and t is not None else None
    if isinstance(expected, (list, tuple)) and len(expected) == 2:
        v, t = _num(expected[0]), _num(expected[1])
        return (v, t) if v is not None and t is not None else None
    s = str(expected)
    m = _TOL_RE.search(s)
    if m:
        return float(m.group(1)), float(m.group(2))
    nums = _NUM_RE.findall(s)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    return None


def evaluate(op: str, actual: Any, expected: Any = None) -> tuple[bool, str]:
    """Evaluate ``actual <op> expected``. Returns ``(ok, human_detail)``."""
    op = (op or "").strip().lower()

    if op == "exists":
        ok = actual is not None and str(actual).strip() != ""
        return ok, f"{'present' if ok else 'missing'}: {actual!r}"
    if op == "is_true":
        ok = _truthy(actual)
        return ok, f"{actual!r} is {'true' if ok else 'false'}"
    if op == "is_false":
        ok = not _truthy(actual)
        return ok, f"{actual!r} is {'false' if ok else 'true'}"

    a = _num(actual)

    if op in ("eq", "ne"):
        b = _num(expected)
        if a is not None and b is not None:
            ok = a == b if op == "eq" else a != b
            return ok, f"{a} {'==' if op == 'eq' else '!='} {b}"
        sa, se = str(actual), str(expected)
        ok = sa == se if op == "eq" else sa != se
        return ok, f"{sa!r} {'==' if op == 'eq' else '!='} {se!r}"

    if op in ("lt", "le", "gt", "ge"):
        b = _num(expected)
        if a is None or b is None:
            return False, f"non-numeric compare: {actual!r} {op} {expected!r}"
        ok = {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]
        return ok, f"{a} {op} {b}"

    if op == "within":
        rng = _parse_range(expected)
        if a is None or rng is None:
            return False, f"bad 'within': {actual!r} in {expected!r}"
        lo, hi = rng
        ok = lo <= a <= hi
        return ok, f"{a} {'within' if ok else 'outside'} {lo}..{hi}"

    if op == "approx":
        ap = _parse_approx(expected)
        if a is None or ap is None:
            return False, f"bad 'approx': {actual!r} ~ {expected!r}"
        value, tol = ap
        ok = abs(a - value) <= tol
        return ok, f"{a} {'~' if ok else '!~'} {value}±{tol}"

    if op in ("contains", "not_contains"):
        sa, se = str(actual), str(expected)
        has = se in sa
        ok = has if op == "contains" else not has
        return ok, f"{sa!r} {'contains' if has else 'lacks'} {se!r}"

    if op in ("matches", "not_matches"):
        sa, se = str(actual), str(expected)
        try:
            hit = re.search(se, sa) is not None
        except re.error as exc:
            return False, f"bad regex {se!r}: {exc}"
        ok = hit if op == "matches" else not hit
        return ok, f"{sa!r} {'matches' if hit else 'does not match'} {se!r}"

    return False, f"unknown op {op!r}"
