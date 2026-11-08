from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from xml.parsers.expat import ExpatError

import xmltodict
from fastapi import HTTPException
from starlette import status

from models.element_id import ElementId, split_element_id
from xmltodict_postprocessor import postprocessor


@dataclass(frozen=True, kw_only=True, slots=True)
class OsmChangeMember:
    type: str
    ref: int
    role: str


@dataclass(frozen=True, kw_only=True, slots=True)
class OsmChangeRelation:
    """The state of a relation stored in an osmChange file, e.g., one previously downloaded from relatify."""

    members: tuple[OsmChangeMember, ...]
    # node lists of the ways created or modified by the file (split ways have negative ids when created)
    ways: tuple[tuple[int, tuple[int, ...]], ...]

    def members_json(self) -> list[dict]:
        """The members in the format of the OSM API JSON responses."""
        return [{'type': m.type, 'ref': m.ref, 'role': m.role} for m in self.members]


def parse_osm_change(text: str, relation_id: int) -> OsmChangeRelation:
    try:
        data = xmltodict.parse(
            text,
            postprocessor=postprocessor,
            force_list=('create', 'modify', 'delete', 'node', 'way', 'relation', 'member', 'tag', 'nd'),
        )
    except (ExpatError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f'Invalid .osc file: {e}') from e

    root = data.get('osmChange') if isinstance(data, dict) else None
    if not isinstance(root, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Invalid .osc file: missing osmChange root element')

    ways: dict[int, tuple[int, ...]] = {}
    relation: dict | None = None

    for action in ('create', 'modify'):
        for block in root.get(action) or ():
            if not block:
                continue
            for way in block.get('way') or ():
                ways[way['@id']] = tuple(nd['@ref'] for nd in way.get('nd') or ())
            for r in block.get('relation') or ():
                if r['@id'] == relation_id:
                    relation = r

    if relation is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f'The .osc file does not modify relation {relation_id}')

    members = tuple(
        OsmChangeMember(type=m['@type'], ref=m['@ref'], role=m.get('@role', '')) for m in relation.get('member') or ()
    )
    if not any(m.type == 'way' for m in members):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Relation is empty, which is not supported')

    return OsmChangeRelation(members=members, ways=tuple(ways.items()))


def match_way_segments(
    way_id: int,
    nodes: Sequence[int],
    segments: Mapping[ElementId, Sequence[int]],
    endpoint_index: Mapping[int, Iterable[ElementId]],
) -> list[ElementId] | None:
    """
    Find the consecutive way segments (see organize_ways) which cover the node list exactly.

    The ways of an osmChange file may be split parts of the OSM ways (relatify splits ways at intersections),
    so they are resolved by their geometry. Segments of the way with the same id are preferred.
    Returns None if the node list can't be covered, e.g., because the OSM data has changed since.
    """
    if len(nodes) < 2:
        return None

    result: list[ElementId] = []
    i = 0

    while i < len(nodes) - 1:
        candidates = sorted(endpoint_index.get(nodes[i], ()), key=lambda s: split_element_id(s).id != way_id)

        for segment_id in candidates:
            segment_nodes = list(segments[segment_id])
            if segment_nodes[0] != nodes[i]:
                segment_nodes.reverse()
            if list(nodes[i : i + len(segment_nodes)]) == segment_nodes:
                result.append(segment_id)
                i += len(segment_nodes) - 1
                break
        else:
            return None

    return result


def build_endpoint_index(segments: Mapping[ElementId, Sequence[int]]) -> dict[int, list[ElementId]]:
    result: dict[int, list[ElementId]] = defaultdict(list)
    for segment_id, nodes in segments.items():
        result[nodes[0]].append(segment_id)
        if nodes[-1] != nodes[0]:
            result[nodes[-1]].append(segment_id)
    return result
