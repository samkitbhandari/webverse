"""Value normalisation.

Contradiction detection is only as good as the comparability of values. Two
documents will happily say "50,000 units/month" and "50k units per month", or
"70 degC" and "158 F", and a naive string comparison sees a conflict where
there is none -- or misses one that is real. Everything numeric therefore
passes through here on the way into the graph.
"""
from __future__ import annotations

import re

from backend.core.models import Quantity

# --- magnitude words / suffixes -------------------------------------------
# Bare "m" and "b" are deliberately absent: in this domain "5 m" is far more
# likely to be five metres than five million, and "20 b" is vanishingly rare.
# Spelled-out forms stay, so "5 million" still resolves.
_MAGNITUDES: dict[str, float] = {
    "k": 1e3, "thousand": 1e3,
    "mn": 1e6, "million": 1e6,
    "bn": 1e9, "billion": 1e9,
    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5,
    "crore": 1e7, "crores": 1e7,
}

# --- unit token -> canonical unit of its family ---------------------------
# Every member of a family maps to the SAME canonical symbol; the numeric
# conversion to that symbol happens in _convert(). Bare "k" is not mapped to
# Kelvin because it collides with the thousands suffix -- spell out "kelvin".
_UNIT_ALIASES: dict[str, str] = {
    # temperature -> degC
    "c": "degC", "degc": "degC", "celsius": "degC", "°c": "degC",
    "f": "degC", "degf": "degC", "fahrenheit": "degC", "°f": "degC",
    "kelvin": "degC",
    # time / latency -> ms
    "ms": "ms", "millisecond": "ms", "milliseconds": "ms", "msec": "ms",
    "s": "ms", "sec": "ms", "secs": "ms", "second": "ms", "seconds": "ms",
    "min": "ms", "mins": "ms", "minute": "ms", "minutes": "ms",
    "h": "ms", "hr": "ms", "hrs": "ms", "hour": "ms", "hours": "ms",
    "day": "ms", "days": "ms", "week": "ms", "weeks": "ms",
    # "month" is safe to treat as a duration: compound rate units are split on
    # "/" before this table is consulted, so "units/month" never reaches it.
    "month": "ms", "months": "ms",
    # throughput / counts
    "tps": "tps", "rps": "tps", "qps": "tps",
    "unit": "unit", "units": "unit", "pcs": "unit", "pieces": "unit",
    "vehicle": "vehicle", "vehicles": "vehicle",
    "user": "user", "users": "user",
    # percent
    "%": "%", "percent": "%", "pct": "%",
    # money (never converted between currencies -- they stay distinct units)
    "inr": "INR", "rs": "INR", "rs.": "INR", "rupee": "INR", "rupees": "INR",
    "₹": "INR", "usd": "USD", "$": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "€": "EUR",
    # energy / electrical / physical
    "kwh": "kWh", "wh": "kWh", "mwh": "kWh",
    "kw": "kW", "w": "kW", "mw": "kW",
    "v": "V", "a": "A", "ah": "Ah",
    "g": "kg", "kg": "kg", "t": "kg", "tonne": "kg", "tonnes": "kg",
    "mm": "m", "cm": "m", "m": "m", "km": "m",
}

#: multiplicative factor to reach the canonical unit of the family
_TO_CANONICAL: dict[str, float] = {
    "ms": 1.0, "s": 1e3, "sec": 1e3, "secs": 1e3, "second": 1e3, "seconds": 1e3,
    "min": 6e4, "mins": 6e4, "minute": 6e4, "minutes": 6e4,
    "h": 3.6e6, "hr": 3.6e6, "hrs": 3.6e6, "hour": 3.6e6, "hours": 3.6e6,
    "day": 8.64e7, "days": 8.64e7, "week": 6.048e8, "weeks": 6.048e8,
    "month": 2.592e9, "months": 2.592e9,
    "g": 1e-3, "kg": 1.0, "t": 1e3, "tonne": 1e3, "tonnes": 1e3,
    "mm": 1e-3, "cm": 1e-2, "m": 1.0, "km": 1e3,
    "w": 1e-3, "kw": 1.0, "mw": 1e3,
    "wh": 1e-3, "kwh": 1.0, "mwh": 1e3,
}

# Polarity words are normalised to booleans so that "certified" and
# "decertified" are recognised as opposing values for the same property --
# which is what makes a semantic contradiction detectable at all.
_BOOL_TRUE = {
    "true", "yes", "supported", "enabled", "active", "compliant", "available",
    "certified", "approved", "operational", "valid", "in force",
}
_BOOL_FALSE = {
    "false", "no", "unsupported", "disabled", "inactive", "deprecated",
    "non-compliant", "noncompliant", "unavailable", "removed", "withdrawn",
    "obsolete", "discontinued", "decertified", "revoked", "suspended",
    "expired", "invalid",
}

_NUM_RE = re.compile(
    r"(?P<sign>[-+])?\s*(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<mag>k|m|mn|bn|b|lakhs?|lac|crores?|thousand|million|billion)?\b",
    re.IGNORECASE,
)


def canonical_unit(raw: str | None) -> str | None:
    """Map a unit string onto its canonical spelling ('C' -> 'degC')."""
    if not raw:
        return None
    u = raw.strip().lower().replace("per ", "/").replace(" ", "")
    if not u:
        return None
    # compound rate units, e.g. units/month, requests/second
    if "/" in u:
        num, _, den = u.partition("/")
        n = _UNIT_ALIASES.get(num, num.rstrip("s") or num)
        d = den.rstrip("s")
        return f"{n}/{d}"
    return _UNIT_ALIASES.get(u, raw.strip())


def _convert(number: float, unit_token: str, canon: str | None) -> float:
    """Apply within-family conversion (F->C, hours->ms, km->m, ...)."""
    tok = unit_token.strip().lower().lstrip("°")
    if canon == "degC":
        if tok in ("f", "degf", "fahrenheit"):
            return (number - 32.0) * 5.0 / 9.0
        if tok == "kelvin":
            return number - 273.15
        return number
    factor = _TO_CANONICAL.get(tok)
    return number * factor if factor is not None else number


def parse_value(raw: str | float | int | None, unit_hint: str | None = None) -> Quantity:
    """Turn whatever a source or an LLM handed us into a comparable Quantity."""
    if raw is None:
        return Quantity(raw="", text=None)
    if isinstance(raw, (int, float)):
        return Quantity(
            raw=str(raw), number=float(raw), unit=canonical_unit(unit_hint)
        )

    text = str(raw).strip()
    if not text:
        return Quantity(raw="")

    low = text.lower().strip(" .")
    if low in _BOOL_TRUE:
        return Quantity(raw=text, boolean=True, text=low)
    if low in _BOOL_FALSE:
        return Quantity(raw=text, boolean=False, text=low)

    m = _NUM_RE.search(text)
    if not m:
        return Quantity(raw=text, text=text, unit=canonical_unit(unit_hint))

    number = float(m.group("num").replace(",", ""))
    if m.group("sign") == "-":
        number = -number
    if mag := m.group("mag"):
        number *= _MAGNITUDES.get(mag.lower(), 1.0)

    # the unit is whatever trails the number, else the caller's hint
    tail = text[m.end():].strip()
    tail_unit = re.split(r"[,;()]| and | or ", tail, maxsplit=1)[0].strip() or None
    # a leading currency symbol counts as a unit too
    lead = text[: m.start()].strip()
    if not tail_unit and lead:
        tail_unit = lead

    unit_token = tail_unit or unit_hint or ""
    canon = canonical_unit(unit_token) or canonical_unit(unit_hint)
    base_token = unit_token.split("/")[0] if unit_token else ""
    number = _convert(number, base_token, canon)

    # Keep the unit in `raw` even when the caller supplied it separately. It is
    # the only record of how the source actually phrased the quantity, and
    # rendering uses it so a document that says "14 days" is not echoed back as
    # "2 weeks".
    raw = text
    if unit_token and unit_token.lower() not in text.lower():
        raw = f"{text} {unit_token}".strip()

    return Quantity(raw=raw, number=number, unit=canon)


def same_unit_family(a: Quantity, b: Quantity) -> bool:
    """Whether two quantities are even comparable."""
    if a.unit is None or b.unit is None:
        return True                      # unitless: assume comparable
    return a.unit == b.unit


def relative_delta(old: Quantity, new: Quantity) -> float | None:
    """Signed fractional change from old to new, or None if incomparable."""
    if not (old.is_numeric and new.is_numeric) or not same_unit_family(old, new):
        return None
    if old.number == 0:
        return None if new.number == 0 else 1.0
    return (new.number - old.number) / abs(old.number)


def satisfies(value: Quantity, operator: str, threshold: Quantity) -> bool | None:
    """Evaluate a constraint. None means 'cannot be decided from these values'."""
    if not (value.is_numeric and threshold.is_numeric):
        if value.boolean is not None and threshold.boolean is not None:
            return (
                value.boolean == threshold.boolean
                if operator in ("EQ", "LTE", "GTE")
                else value.boolean != threshold.boolean
            )
        return None
    if not same_unit_family(value, threshold):
        return None
    v, t = value.number, threshold.number
    return {
        "LT": v < t, "LTE": v <= t,
        "GT": v > t, "GTE": v >= t,
        "EQ": abs(v - t) < 1e-9, "NEQ": abs(v - t) >= 1e-9,
    }.get(operator.upper())
