"""2D geometry helpers."""

from dataclasses import dataclass


@dataclass
class Point:
    """A point in 2D space."""

    x: float
    y: float

    def distance_to(self, other):
        """Euclidean distance to another point."""
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5
