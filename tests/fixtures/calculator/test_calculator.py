import pytest

from calculator import divide


def test_divide_returns_quotient() -> None:
    assert divide(8, 2) == 4


def test_divide_rejects_zero_divisor_with_domain_error() -> None:
    with pytest.raises(ValueError, match="division by zero"):
        divide(8, 0)
