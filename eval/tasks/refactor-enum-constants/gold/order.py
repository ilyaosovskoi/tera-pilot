"""Order helpers — statuses come from the OrderStatus enum."""

from status import OrderStatus


def describe(order):
    """Return a human-readable description of an order's status."""
    if order["status"] == OrderStatus.NEW:
        return "New order"
    if order["status"] == OrderStatus.SHIPPED:
        return "Shipped"
    return "Cancelled"
