import pytest

from ynab_cli.errors import CliError
from ynab_cli.money import from_milliunits, to_milliunits


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12.50", 12500),
        ("-0.001", -1),
        ("0", 0),
        ("6744.480", 6744480),
        ("9007199254740.991", 9007199254740991),
    ],
)
def test_exact_money_roundtrip(value, expected):
    assert to_milliunits(value) == expected
    assert to_milliunits(from_milliunits(expected)) == expected


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "1e3", "1.0001", "£12", "1,000", "9007199254740.992"]
)
def test_invalid_amount_never_silently_rounds(value):
    with pytest.raises(CliError, match="amount|Amount|decimal"):
        to_milliunits(value)
