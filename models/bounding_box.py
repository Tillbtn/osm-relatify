from dataclasses import dataclass
from math import cos, degrees, radians
from typing import Self

from config import DOWNLOAD_RELATION_GRID_SIZE
from models.download_history import Cell


@dataclass(frozen=True, slots=True)
class BoundingBox:
    minlat: float
    minlon: float
    maxlat: float
    maxlon: float

    def __str__(self) -> str:
        return f'{self.minlat:.6f},{self.minlon:.6f},{self.maxlat:.6f},{self.maxlon:.6f}'

    def contains(self, lat: float, lon: float) -> bool:
        return self.minlat <= lat <= self.maxlat and self.minlon <= lon <= self.maxlon

    def intersects_segment(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """Check if the segment between the two lat/lon points intersects the bounding box (Liang-Barsky)."""
        t0, t1 = 0.0, 1.0
        dlat, dlon = b[0] - a[0], b[1] - a[1]

        for p, q in (
            (-dlat, a[0] - self.minlat),
            (dlat, self.maxlat - a[0]),
            (-dlon, a[1] - self.minlon),
            (dlon, self.maxlon - a[1]),
        ):
            if p == 0:
                if q < 0:
                    return False
                continue
            t = q / p
            if p < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)

        return True

    def split(self) -> tuple[Self, Self]:
        """Split the bounding box into two halves along its longer side."""
        if self.maxlat - self.minlat > self.maxlon - self.minlon:
            midlat = (self.minlat + self.maxlat) / 2
            return (
                BoundingBox(minlat=self.minlat, minlon=self.minlon, maxlat=midlat, maxlon=self.maxlon),
                BoundingBox(minlat=midlat, minlon=self.minlon, maxlat=self.maxlat, maxlon=self.maxlon),
            )
        midlon = (self.minlon + self.maxlon) / 2
        return (
            BoundingBox(minlat=self.minlat, minlon=self.minlon, maxlat=self.maxlat, maxlon=midlon),
            BoundingBox(minlat=self.minlat, minlon=midlon, maxlat=self.maxlat, maxlon=self.maxlon),
        )

    def extend(self, unit_meters: float | None = None, *, unit_degrees: float | None = None) -> Self:
        if unit_meters is not None:
            earth_radius = 6_371_000
            lat_delta = degrees(unit_meters / earth_radius)
            lng_delta = degrees(unit_meters / (earth_radius * cos(radians(self.minlat))))
        elif unit_degrees is not None:
            lat_delta = unit_degrees
            lng_delta = unit_degrees
        else:
            raise ValueError('Either unit_meters or unit_degrees must be specified')

        return BoundingBox(
            minlat=self.minlat - lat_delta,
            minlon=self.minlon - lng_delta,
            maxlat=self.maxlat + lat_delta,
            maxlon=self.maxlon + lng_delta,
        )

    def get_grid_cells(self, *, expand: int = 0) -> set[Cell]:
        min_x = int(self.minlon // DOWNLOAD_RELATION_GRID_SIZE)
        min_y = int(self.minlat // DOWNLOAD_RELATION_GRID_SIZE)
        max_x = int(self.maxlon // DOWNLOAD_RELATION_GRID_SIZE)
        max_y = int(self.maxlat // DOWNLOAD_RELATION_GRID_SIZE)

        return {
            Cell(x, y)
            for x in range(min_x - expand, max_x + 1 + expand)
            for y in range(min_y - expand, max_y + 1 + expand)
        }

    @classmethod
    def from_grid_cell(cls, x: int, y: int, x_max: int | None = None, y_max: int | None = None) -> Self:
        return BoundingBox(
            minlat=y * DOWNLOAD_RELATION_GRID_SIZE,
            minlon=x * DOWNLOAD_RELATION_GRID_SIZE,
            maxlat=((y_max or y) + 1) * DOWNLOAD_RELATION_GRID_SIZE,
            maxlon=((x_max or x) + 1) * DOWNLOAD_RELATION_GRID_SIZE,
        )
