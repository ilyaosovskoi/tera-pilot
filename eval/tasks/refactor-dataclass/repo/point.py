"""2D geometry helpers."""


class Point:
    """A point in 2D space."""

    def __init__(self, x, y):
        self.x = x
        self.y = y

    def distance_to(self, other):
        """Euclidean distance to another point."""
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5
