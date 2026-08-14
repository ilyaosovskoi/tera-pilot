"""Order statuses as an enum."""

from enum import Enum


class OrderStatus(Enum):
    NEW = "new"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"
