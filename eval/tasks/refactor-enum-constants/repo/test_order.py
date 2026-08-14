"""Tests for the refactored order module."""

from order import describe
from status import OrderStatus


def test_enum_members():
    assert OrderStatus.NEW.value == "new"
    assert OrderStatus.SHIPPED.value == "shipped"
    assert OrderStatus.CANCELLED.value == "cancelled"


def test_describe_uses_enum():
    assert describe({"status": OrderStatus.NEW}) == "New order"
    assert describe({"status": OrderStatus.CANCELLED}) == "Cancelled"
