"""Order helpers. Statuses are bare string constants."""

STATUS_NEW = "new"
STATUS_SHIPPED = "shipped"
STATUS_CANCELLED = "cancelled"


def describe(order):
    """Return a human-readable description of an order's status."""
    if order["status"] == STATUS_NEW:
        return "New order"
    if order["status"] == STATUS_SHIPPED:
        return "Shipped"
    return "Cancelled"
