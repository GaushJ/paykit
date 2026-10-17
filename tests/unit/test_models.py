import pytest
from pydantic import ValidationError

from paykit.core.models import Money


def test_money_normalizes_currency_case() -> None:
    m = Money(amount=100, currency="inr")
    assert m.currency == "INR"


def test_money_rejects_non_positive_amount() -> None:
    with pytest.raises(ValidationError):
        Money(amount=0, currency="INR")
    with pytest.raises(ValidationError):
        Money(amount=-1, currency="INR")


def test_money_rejects_bad_currency_code() -> None:
    with pytest.raises(ValidationError):
        Money(amount=100, currency="RUPEE")
