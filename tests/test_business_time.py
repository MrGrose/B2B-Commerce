from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from b2b_commerce.invoices.service import compute_invoice_expires_at

MOSCOW = ZoneInfo("Europe/Moscow")


def _msk(y, m, d, h=10, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=MOSCOW).astimezone(UTC)


@pytest.mark.parametrize(
    ("created", "expected_day", "expected_month"),
    [
        (_msk(2026, 8, 26), 28, 8),  # Wed -> Fri
        (_msk(2026, 8, 28), 1, 9),  # Fri -> Tue (Sep)
        (_msk(2026, 8, 25), 27, 8),  # Tue -> Thu
        (_msk(2026, 8, 29), 1, 9),  # Sat -> Tue Sep 1 (skip weekend)
    ],
)
def test_compute_invoice_expires_at_two_business_days(created, expected_day, expected_month):
    expires = compute_invoice_expires_at(created, business_days=2)
    local = expires.astimezone(MOSCOW)
    assert local.weekday() < 5
    assert local.day == expected_day
    assert local.month == expected_month
    assert local.hour == 23
    assert local.minute == 59
    assert local.second == 59


def test_compute_invoice_expires_at_creation_day_not_counted():
    created = _msk(2026, 8, 26, 23, 30)  # Wed evening
    expires = compute_invoice_expires_at(created, business_days=2)
    local = expires.astimezone(MOSCOW)
    assert local.weekday() == 4  # Fri, not Thu
    assert local.day == 28


def test_compute_invoice_expires_at_respects_business_days_param():
    created = _msk(2026, 8, 26)  # Wed
    expires_one = compute_invoice_expires_at(created, business_days=1)
    expires_two = compute_invoice_expires_at(created, business_days=2)
    assert expires_one.astimezone(MOSCOW).day == 27  # Thu
    assert expires_two.astimezone(MOSCOW).day == 28  # Fri


def test_compute_invoice_expires_at_rejects_zero_days():
    with pytest.raises(ValueError, match="business_days"):
        compute_invoice_expires_at(_msk(2026, 8, 26), business_days=0)
