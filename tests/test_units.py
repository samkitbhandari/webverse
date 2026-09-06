"""Value normalisation.

These are the tests that matter most for correctness of everything downstream:
if two equivalent quantities do not normalise to the same number and unit, the
contradiction engine either invents conflicts or misses real ones.
"""
from __future__ import annotations

import pytest

from backend.core.units import (
    canonical_unit, parse_value, relative_delta, same_unit_family, satisfies,
)


@pytest.mark.parametrize(
    "raw,number,unit",
    [
        ("50,000 units/month", 50_000, "unit/month"),
        ("50k units per month", 50_000, "unit/month"),
        ("70 degC", 70, "degC"),
        ("70 C", 70, "degC"),
        ("158 F", 70, "degC"),               # converted, not just relabelled
        ("180 ms", 180, "ms"),
        ("2.5 s", 2500, "ms"),
        ("14 days", 14 * 86_400_000, "ms"),
        ("20 lakh", 2_000_000, None),
        ("3120 INR", 3120, "INR"),
        ("99 percent", 99, "%"),
    ],
)
def test_equivalent_quantities_normalise_together(raw, number, unit):
    q = parse_value(raw)
    assert q.number == pytest.approx(number, rel=1e-6)
    assert q.unit == unit


def test_currency_symbol_before_the_number_is_still_a_unit():
    q = parse_value("₹20 lakh")
    assert q.number == pytest.approx(2_000_000)
    assert q.unit == "INR"


@pytest.mark.parametrize("raw", ["Phase 2", "rev.3", "A7", "BP-7", "UNECE R100"])
def test_identifiers_are_not_quantities(raw):
    """A bare integer with no unit is an identifier, not a measurement."""
    q = parse_value(raw)
    assert q.number is None or q.unit is not None


def test_booleans_are_recognised_by_polarity():
    assert parse_value("deprecated").boolean is False
    assert parse_value("supported").boolean is True


def test_relative_delta_is_signed_and_fractional():
    old, new = parse_value("50000"), parse_value("31000")
    assert relative_delta(old, new) == pytest.approx(-0.38)
    assert relative_delta(new, old) == pytest.approx(0.6129, rel=1e-3)


def test_incomparable_units_do_not_compare():
    assert not same_unit_family(parse_value("70 degC"), parse_value("70 ms"))
    assert relative_delta(parse_value("70 degC"), parse_value("70 ms")) is None


@pytest.mark.parametrize(
    "value,op,threshold,expected",
    [
        ("180 ms", "LT", "100 ms", False),
        ("68 degC", "LTE", "60 degC", False),
        ("55 degC", "LTE", "60 degC", True),
        ("99.3 percent", "GTE", "99 percent", True),
    ],
)
def test_constraint_evaluation(value, op, threshold, expected):
    assert satisfies(parse_value(value), op, parse_value(threshold)) is expected


def test_constraint_across_unit_families_is_undecidable():
    """Returning False here would report a violation that does not exist."""
    assert satisfies(parse_value("70 degC"), "LT", parse_value("100 ms")) is None


def test_durations_render_back_into_readable_units():
    assert parse_value("14 days").render() == "14 days"
    assert parse_value("2.5 s").render() == "2.5 seconds"


def test_canonical_unit_collapses_synonyms():
    assert canonical_unit("Celsius") == canonical_unit("degC") == "degC"
    assert canonical_unit("units per month") == "unit/month"
