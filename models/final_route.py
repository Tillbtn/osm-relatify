from dataclasses import dataclass, field
from enum import Enum

from models.fetch_relation import FetchRelationBusStopCollection, FetchRelationElement
from models.relation_member import RelationMember


@dataclass(frozen=True, kw_only=True, slots=True)
class FinalRouteWay:
    way: FetchRelationElement
    reversed_latLngs: bool


class WarningSeverity(int, Enum):
    LOW = 0
    HIGH = 1
    UNCHANGED = 10


@dataclass(frozen=True, kw_only=True, slots=True)
class FinalRouteWarning:
    severity: WarningSeverity
    message: str
    extra: tuple = field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True, slots=True)
class FinalRouteSection:
    """
    An additional part of the route, which is not part of the main (forward) path.

    'backward': the return leg of a split section, ways are in the direction of travel (merge point -> split point);
    position is the index in FinalRoute.ways before which the leg belongs (None if it could not be attached)
    anything else: a branch (e.g., 'alternative', 'excursion'), ways are in the direction of travel
    """

    role: str
    ways: tuple[FinalRouteWay, ...]
    latLngs: tuple[tuple[float, float], ...]
    position: int | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class FinalRoute:
    # the main path in the forward direction of travel
    ways: tuple[FinalRouteWay, ...]
    latLngs: tuple[tuple[float, float], ...]
    busStops: tuple[FetchRelationBusStopCollection, ...]
    tags: dict[str, str]

    # routes with split sections or branches: return legs and branches
    sections: tuple[FinalRouteSection, ...] = ()

    # remaining parts of split ways, which are not members of the route
    extraWaysToUpdate: tuple[FetchRelationElement, ...] = None

    members: tuple[RelationMember, ...] = None

    warnings: tuple[FinalRouteWarning, ...] = None

    @property
    def roundtrip(self) -> bool:
        return self.tags.get('roundtrip', 'no') == 'yes'
