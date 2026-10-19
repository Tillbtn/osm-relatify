import asyncio
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from itertools import chain, pairwise
from typing import NamedTuple

from asyncache import cached
from cachetools import TTLCache
from fastapi import HTTPException
from httpx import HTTPStatusError
from starlette import status

from bus_collection_builder import build_bus_stop_collections
from config import (
    DOWNLOAD_RELATION_GRID_CELL_EXPAND,
    DOWNLOAD_RELATION_MAP_CONCURRENCY,
    DOWNLOAD_RELATION_SOURCE,
    DOWNLOAD_RELATION_WAY_BB_EXPAND,
    OVERPASS_API_INTERPRETER,
)
from models.bounding_box import BoundingBox
from models.bounding_box_collection import BoundingBoxCollection
from models.download_history import Cell, DownloadHistory
from models.element_id import ElementId, element_id
from models.fetch_relation import FetchRelationBusStop, FetchRelationBusStopCollection, FetchRelationElement
from openstreetmap import OpenStreetMap
from utils import HTTP

# TODO: right hand side detection by querying roundabouts, and first/last bus stop


class QueryParentsResult(NamedTuple):
    id_relations_map: dict[int, list[dict]]
    ways_map: dict[int, dict]


def split_by_count(elements: Iterable[dict]) -> list[list[dict]]:
    result = []
    current_split = []

    for e in elements:
        if e['type'] == 'count':
            result.append(current_split)
            current_split = []
        else:
            current_split.append(e)

    assert not current_split, 'Last element must be count type'
    return result


def build_query(
    relation_id: int,
    cell_bbs: Sequence[BoundingBox],
    cell_bbs_expanded: Sequence[BoundingBox],
    timeout: int,
    route_type: str,
) -> str:
    if route_type == 'bus':
        return (
            f'[out:json][timeout:{timeout}];'
            f'(' + ''.join(f'way[highway][!footway]({bb});' for bb in cell_bbs) + ');'
            'out body qt;'
            'out count;'
            '>;'
            'out skel qt;'
            'out count;'
            '(' + ''.join(f'node[highway=turning_circle]({bb});' for bb in cell_bbs) + ');'
            'out tags qt;'
            'out count;'
            + ''.join(
                f'node[highway=bus_stop][public_transport=platform][name]({bb});'
                f'out tags center qt;'
                f'nwr[highway=platform][public_transport=platform][name]({bb});'
                f'out tags center qt;'
                f'nwr[highway=platform][public_transport=platform][ref]({bb});'
                f'out tags center qt;'
                f'node[public_transport=stop_position][name]({bb});'
                f'out tags center qt;'
                for bb in cell_bbs_expanded
            )
            + 'out count;'
            '(' + ''.join(f'rel[public_transport=stop_area]({bb});' for bb in cell_bbs_expanded) + ')->.r;'
            '.r out body qt;'
            '.r out count;'
            'node(r.r:platform);'
            'out tags center qt;'
            'way(r.r:platform);'
            'out tags center qt;'
            'rel(r.r:platform);'
            'out tags center qt;'
            'out count;'
            'node(r.r:stop);'
            'out tags center qt;'
            'out count;'
        )

    if route_type == 'tram':
        return (
            f'[out:json][timeout:{timeout}];'
            f'(' + ''.join(f'way[railway=tram]({bb});' for bb in cell_bbs) + ');'
            'out body qt;'
            'out count;'
            '>;'
            'out skel qt;'
            'out count;'
            '(' + ''.join(f'node[highway=turning_circle]({bb});' for bb in cell_bbs) + ');'
            'out tags qt;'
            'out count;'
            + ''.join(
                f'node[railway=tram_stop][public_transport=stop_position][name]({bb});'
                f'out tags center qt;'
                f'nwr[railway=platform][public_transport=platform][name]({bb});'
                f'out tags center qt;'
                f'nwr[railway=platform][public_transport=platform][ref]({bb});'
                f'out tags center qt;'
                f'nwr[tram][public_transport=platform][name]({bb});'
                f'out tags center qt;'
                for bb in cell_bbs_expanded
            )
            + 'out count;'
            '(' + ''.join(f'rel[public_transport=stop_area]({bb});' for bb in cell_bbs_expanded) + ')->.r;'
            '.r out body qt;'
            '.r out count;'
            'node(r.r:platform);'
            'out tags center qt;'
            'way(r.r:platform);'
            'out tags center qt;'
            'rel(r.r:platform);'
            'out tags center qt;'
            'out count;'
            'node(r.r:stop);'
            'out tags center qt;'
            'out count;'
        )

    if route_type == 'bicycle':
        # bicycle routes have no stops
        # sidewalks and crossings are only relevant if bicycles are explicitly allowed (or already members)
        return (
            f'[out:json][timeout:{timeout}];'
            f'rel({relation_id})->.r;'
            f'('
            + ''.join(
                f'way[highway][!footway]({bb});way[highway][footway][bicycle]({bb});way(r.r)({bb});' for bb in cell_bbs
            )
            + ');'
            'out body qt;'
            'out count;'
            '>;'
            'out skel qt;'
            'out count;'
            '(' + ''.join(f'node[highway=turning_circle]({bb});' for bb in cell_bbs) + ');'
            'out tags qt;'
            'out count;'
            'way(r.r);'
            'out ids qt;'
            'out count;'
        )

    raise NotImplementedError(f'Unsupported route type {route_type!r}')


def build_map_elements_split(
    elements: Iterable[dict],
    relation_way_ids: frozenset[int],
    cell_bbs: Sequence[BoundingBox],
    cell_bbs_expanded: Sequence[BoundingBox],
    route_type: str,
) -> list[list[dict]]:
    """
    Emulate the result of build_query on raw OSM API map data.

    The filters must be kept in sync with build_query.
    """
    nodes: dict[int, dict] = {}
    ways: dict[int, dict] = {}
    relations: dict[int, dict] = {}
    type_map = {'node': nodes, 'way': ways, 'relation': relations}

    for e in elements:
        type_map[e['type']][e['id']] = e

    def nodes_within(bbs: Sequence[BoundingBox]) -> frozenset[int]:
        return frozenset(n['id'] for n in nodes.values() if any(bb.contains(n['lat'], n['lon']) for bb in bbs))

    cell_node_ids = nodes_within(cell_bbs)
    expanded_node_ids = nodes_within(cell_bbs_expanded)

    def is_within(e: dict, node_ids: frozenset[int], bbs: Sequence[BoundingBox]) -> bool:
        if e['type'] == 'node':
            return e['id'] in node_ids
        if e['type'] == 'way':
            if any(n in node_ids for n in e['nodes']):
                return True
            # like overpass, also match ways crossing the bounding box without a node inside
            way_coords = tuple(coords(e))
            return any(bb.intersects_segment(a, b) for a, b in pairwise(way_coords) for bb in bbs)
        return any(
            (m['type'] == 'node' and m['ref'] in node_ids)
            or (m['type'] == 'way' and (w := ways.get(m['ref'])) is not None and is_within(w, node_ids, bbs))
            for m in e['members']
        )

    def coords(e: dict) -> Iterator[tuple[float, float]]:
        if e['type'] == 'node':
            yield e['lat'], e['lon']
        elif e['type'] == 'way':
            for n in e['nodes']:
                if (node := nodes.get(n)) is not None:
                    yield node['lat'], node['lon']
        else:
            for m in e['members']:
                if m['type'] != 'relation' and (member := type_map[m['type']].get(m['ref'])) is not None:
                    yield from coords(member)

    def out_tags_center(e: dict) -> dict | None:
        tags = e.get('tags', {})
        if e['type'] == 'node':
            return {'type': 'node', 'id': e['id'], 'lat': e['lat'], 'lon': e['lon'], 'tags': tags}
        e_coords = tuple(coords(e))
        if not e_coords:
            return None
        lats, lons = zip(*e_coords)
        center = {'lat': round((min(lats) + max(lats)) / 2, 7), 'lon': round((min(lons) + max(lons)) / 2, 7)}
        return {'type': e['type'], 'id': e['id'], 'center': center, 'tags': tags}

    def is_candidate_way(way: dict) -> bool:
        tags = way.get('tags', {})
        if route_type == 'bus':
            return 'highway' in tags and 'footway' not in tags
        if route_type == 'tram':
            return tags.get('railway') == 'tram'
        if route_type == 'bicycle':
            return ('highway' in tags and ('footway' not in tags or 'bicycle' in tags)) or way['id'] in relation_way_ids
        raise NotImplementedError(f'Unsupported route type {route_type!r}')

    road_ways = sorted(
        (w for w in ways.values() if is_candidate_way(w) and is_within(w, cell_node_ids, cell_bbs)),
        key=lambda w: w['id'],
    )
    road_node_ids = dict.fromkeys(chain.from_iterable(w['nodes'] for w in road_ways))
    turning_circles = sorted(
        (
            n
            for n in nodes.values()
            if n['id'] in cell_node_ids and n.get('tags', {}).get('highway') == 'turning_circle'
        ),
        key=lambda n: n['id'],
    )

    result = [
        [{'type': 'way', 'id': w['id'], 'nodes': w['nodes'], 'tags': w.get('tags', {})} for w in road_ways],
        [
            {'type': 'node', 'id': n['id'], 'lat': n['lat'], 'lon': n['lon']}
            for n in (nodes[n_id] for n_id in road_node_ids if n_id in nodes)
        ],
        [{'type': 'node', 'id': n['id'], 'tags': n['tags']} for n in turning_circles],
    ]

    if route_type == 'bicycle':
        result.append([{'type': 'way', 'id': way_id} for way_id in sorted(relation_way_ids)])
        return result

    def is_stop(e: dict) -> bool:
        tags = e.get('tags')
        if not tags:
            return False
        is_node = e['type'] == 'node'
        public_transport = tags.get('public_transport')
        if route_type == 'bus':
            return (
                (is_node and tags.get('highway') == 'bus_stop' and public_transport == 'platform' and 'name' in tags)
                or (
                    tags.get('highway') == 'platform'
                    and public_transport == 'platform'
                    and ('name' in tags or 'ref' in tags)
                )
                or (is_node and public_transport == 'stop_position' and 'name' in tags)
            )
        if route_type == 'tram':
            return (
                (
                    is_node
                    and tags.get('railway') == 'tram_stop'
                    and public_transport == 'stop_position'
                    and 'name' in tags
                )
                or (
                    tags.get('railway') == 'platform'
                    and public_transport == 'platform'
                    and ('name' in tags or 'ref' in tags)
                )
                or ('tram' in tags and public_transport == 'platform' and 'name' in tags)
            )
        raise NotImplementedError(f'Unsupported route type {route_type!r}')

    stops = [
        out
        for e in chain(nodes.values(), ways.values(), relations.values())
        if is_stop(e) and is_within(e, expanded_node_ids, cell_bbs_expanded) and (out := out_tags_center(e)) is not None
    ]

    stop_areas = sorted(
        (
            r
            for r in relations.values()
            if r.get('tags', {}).get('public_transport') == 'stop_area'
            and is_within(r, expanded_node_ids, cell_bbs_expanded)
        ),
        key=lambda r: r['id'],
    )
    stop_area_platforms: dict[tuple[str, int], dict] = {}
    stop_area_stops: dict[tuple[str, int], dict] = {}

    for r in stop_areas:
        for m in r['members']:
            key = (m['type'], m['ref'])
            if m['role'] == 'platform':
                target = stop_area_platforms
            elif m['role'] == 'stop' and m['type'] == 'node':
                target = stop_area_stops
            else:
                continue
            if key in target or (member := type_map[m['type']].get(m['ref'])) is None:
                continue
            if (out := out_tags_center(member)) is not None:
                target[key] = out

    def is_member_available(m: dict) -> bool:
        key = (m['type'], m['ref'])
        if m['role'] == 'platform':
            return key in stop_area_platforms
        if m['role'] == 'stop':
            return key in stop_area_stops
        return True

    result.extend(
        (
            stops,
            # members outside the downloaded area are unavailable, unlike with overpass
            [
                {
                    'type': 'relation',
                    'id': r['id'],
                    'members': [m for m in r['members'] if is_member_available(m)],
                    'tags': r.get('tags', {}),
                }
                for r in stop_areas
            ],
            list(stop_area_platforms.values()),
            list(stop_area_stops.values()),
        )
    )
    return result


def is_routable(tags: dict[str, str], route_type: str) -> bool:
    if route_type == 'bus':
        highway_valid = tags['highway'] in {
            'residential',
            'service',
            'unclassified',
            'tertiary',
            'tertiary_link',
            'secondary',
            'secondary_link',
            'primary',
            'primary_link',
            'living_street',
            'trunk',
            'trunk_link',
            'motorway',
            'motorway_link',
            'motorway_junction',
            'road',
            'busway',
            'bus_guideway',
        }

        highway_designated_valid = tags['highway'] in {
            'pedestrian',
        }

        service_valid = tags.get('service', 'no') not in {
            'driveway',
            'parking_aisle',
            'alley',
            'emergency_access',
        }

        access_designated = False
        access_valid = True

        if 'bus:conditional' in tags:
            access_designated = access_valid = True
        elif 'bus' in tags:
            access_designated = access_valid = tags['bus'] not in {'no'}
        elif 'psv' in tags:
            access_designated = access_valid = tags['psv'] not in {'no'}
        elif 'motor_vehicle' in tags:
            access_valid = tags['motor_vehicle'] not in {'private', 'customers', 'no'}
        elif 'access' in tags:
            access_valid = tags['access'] not in {'private', 'customers', 'no'}

        noarea_valid = tags.get('area', 'no') in {'no'}

        return all(
            (
                (highway_valid or (highway_designated_valid and access_designated)),
                (service_valid or access_designated),
                access_valid,
                noarea_valid,
            )
        )

    if route_type == 'tram':
        # all overpass-fetched elements are routable
        return True

    if route_type == 'bicycle':
        # be permissive: ways which are not routable can't be added to the route (current members are always kept)
        highway_valid = tags['highway'] in {
            'cycleway',
            'residential',
            'service',
            'unclassified',
            'tertiary',
            'tertiary_link',
            'secondary',
            'secondary_link',
            'primary',
            'primary_link',
            'trunk',
            'trunk_link',
            'living_street',
            'road',
            'track',
            'path',
            'busway',
        }

        highway_designated_valid = tags['highway'] in {
            'footway',
            'pedestrian',
            'bridleway',
            'steps',
            'corridor',
            'motorway',
            'motorway_link',
        }

        access_designated = False
        access_valid = True

        if 'bicycle' in tags:
            access_designated = tags['bicycle'] in {'yes', 'designated', 'permissive', 'dismount'}
            access_valid = tags['bicycle'] not in {'no', 'private'}
        elif 'vehicle' in tags:
            access_valid = tags['vehicle'] not in {'private', 'no'}
        elif 'access' in tags:
            access_valid = tags['access'] not in {'private', 'no'}

        motorroad_valid = tags.get('motorroad', 'no') in {'no'}
        noarea_valid = tags.get('area', 'no') in {'no'}

        return all(
            (
                (highway_valid or (highway_designated_valid and access_designated)),
                (motorroad_valid or access_designated),
                access_valid,
                noarea_valid,
            )
        )

    raise NotImplementedError(f'Unsupported route type {route_type!r}')


def is_oneway(tags: dict[str, str], route_type: str) -> bool:
    # TODO: it would be nice to support oneway=-1

    roundabout_valid = False

    if 'junction' in tags:
        roundabout_valid = tags['junction'] in {'roundabout'}

    oneway_valid = roundabout_valid

    if route_type == 'bicycle':
        if 'oneway:bicycle' in tags:
            oneway_valid = tags['oneway:bicycle'] in {'yes'}
        elif tags.get('cycleway', '').startswith('opposite'):
            # deprecated contraflow tagging, equivalent to oneway:bicycle=no
            oneway_valid = False
        elif 'oneway' in tags:
            oneway_valid = tags['oneway'] in {'yes'}
    elif 'oneway:bus' in tags:
        oneway_valid = tags['oneway:bus'] in {'yes'}
    elif 'oneway:psv' in tags:
        oneway_valid = tags['oneway:psv'] in {'yes'}
    elif 'oneway' in tags:
        oneway_valid = tags['oneway'] in {'yes'}

    return oneway_valid


def is_roundabout(tags: dict[str, str]) -> bool:
    return tags.get('junction', 'no') in {'roundabout'}


def is_bus_explicit(tags: dict[str, str]) -> bool:
    return tags.get('bus') == 'yes' or tags.get('trolleybus') == 'yes'


def is_any_rail_related(tags: dict[str, str]) -> bool:
    rail_valid = 'railway' in tags
    tram_valid = tags.get('tram', 'no') in {'yes'}
    train_valid = tags.get('train', 'no') in {'yes'}
    subway_valid = tags.get('subway', 'no') in {'yes'}
    return any((rail_valid, tram_valid, train_valid, subway_valid))


def is_tram_element(tags: dict[str, str]) -> bool:
    rail_valid = 'railway' in tags
    tram_valid = tags.get('tram', 'no') in {'yes'}
    train_valid = tags.get('train', 'no') in {'yes'}
    subway_valid = tags.get('subway', 'no') in {'yes'}
    return tram_valid or (rail_valid and not train_valid and not subway_valid)


def _merge_relation_tags(element: dict, relation: dict, extra: dict) -> None:
    element['tags'] = {
        **relation.get('tags', {}),
        **element.get('tags', {}),
        **extra,
    }


def merge_relations_tags(relations: Iterable[dict], elements: Iterable[dict], role: str, public_transport: str) -> None:
    element_map = {(e['type'], e['id']): e for e in elements}

    for relation in sorted(relations, key=lambda r: r['id']):
        for member in (m for m in relation['members'] if m['role'] == role):
            platform = element_map.get((member['type'], member['ref']), None)

            if platform is None:
                print(f'🚧 Warning: Platform {member["type"]}/{member["ref"]} not found in map')
                continue

            _merge_relation_tags(platform, relation, {'public_transport': public_transport})


def _create_node_counts(ways: list[dict]) -> Counter[int]:
    node_counts: Counter[int] = Counter()
    for way in ways:
        node_counts.update(way['nodes'])
    return node_counts


def _split_way_on_intersection(way: dict, node_counts: Mapping[int, int]) -> list[list[int]]:
    segments: list[list[int]] = []
    current_segment: list[int] = []

    for node in way['nodes']:
        current_segment.append(node)

        if node_counts[node] > 1 and len(current_segment) > 1:
            segments.append(current_segment)
            current_segment = [node]

    if len(current_segment) > 1:
        segments.append(current_segment)

    return segments


def organize_ways(ways: list[dict], turn_in_place_nodes: set[int]) -> tuple[list[dict], dict[ElementId, set[ElementId]], dict[int, list[ElementId]]]:
    node_counts = _create_node_counts(ways)
    node_to_way_map = defaultdict(set)

    split_ways: list[dict] = []
    connected_ways_map: dict[ElementId, set[ElementId]] = defaultdict(set)
    id_map = defaultdict(list)

    for way in ways:
        split_segments = _split_way_on_intersection(way, node_counts)

        for extra_num, segment in enumerate(split_segments, 1):
            extra_num = extra_num if len(split_segments) > 1 else None
            max_num = len(split_segments) if extra_num is not None else None

            split_way = {
                **way,
                'id': element_id(way['id'], extra_num=extra_num, max_num=max_num),
                'nodes': segment,
                '_turn_in_place_start': segment[0] in turn_in_place_nodes,
                '_turn_in_place_end': segment[-1] in turn_in_place_nodes,
            }

            split_ways.append(split_way)
            id_map[way['id']].append(split_way['id'])

            for node in segment:
                if node_counts[node] > 1:
                    for other_way_id in node_to_way_map[node]:
                        connected_ways_map[split_way['id']].add(other_way_id)
                        connected_ways_map[other_way_id].add(split_way['id'])
                    node_to_way_map[node].add(split_way['id'])

    return split_ways, connected_ways_map, id_map


def preprocess_elements(elements: Iterable[dict]) -> tuple[dict, ...]:
    # deduplicate by type/id
    result: tuple[dict, ...] = tuple({(e['type'], e['id']): e for e in elements}.values())
    # extract center
    for e in result:
        center: dict | None = e.get('center')
        if center is not None:
            e.update(center)
    return result


def optimize_cells_and_get_bbs(
    cells: Sequence[Cell],
    *,
    start_horizontal: bool,
) -> tuple[Sequence[BoundingBox], Sequence[BoundingBox]]:
    def merge(sorted: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
        result = []
        current = sorted[0]

        for next in sorted[1:]:
            # merge horizontally
            if current[2] + 1 == next[0] and current[1] == next[1] and current[3] == next[3]:
                current = (current[0], current[1], next[2], current[3])

            # merge vertically
            elif current[3] + 1 == next[1] and current[0] == next[0] and current[2] == next[2]:
                current = (current[0], current[1], current[2], next[3])

            # add to merged cells if cells can't be merged
            else:
                result.append(current)
                current = next

        # add the last cell
        result.append(current)

        return result

    cells_bounds = ((c.x, c.y, c.x, c.y) for c in cells)

    if start_horizontal:
        cells_bounds = sorted(cells_bounds, key=lambda c: (c[1], c[0]))
    else:
        cells_bounds = sorted(cells_bounds, key=lambda c: (c[0], c[1]))

    cells_bounds = merge(cells_bounds)

    if start_horizontal:
        cells_bounds = sorted(cells_bounds, key=lambda c: (c[0], c[1]))
    else:
        cells_bounds = sorted(cells_bounds, key=lambda c: (c[1], c[0]))

    cells_bounds = merge(cells_bounds)

    bbs = tuple(BoundingBox.from_grid_cell(*c) for c in cells_bounds)

    return bbs, tuple(bb.extend(unit_degrees=DOWNLOAD_RELATION_GRID_CELL_EXPAND) for bb in bbs)


def get_download_triggers(
    bbc: BoundingBoxCollection,
    cells: Sequence[Cell],
    ways: dict[ElementId, FetchRelationElement],
) -> dict[ElementId, tuple[Cell, ...]]:
    cells_set = frozenset(cells)
    result = {}

    for way_id, way in ways.items():
        way_new_cells = set()

        for latLng in way.latLngs:
            if bbc.contains(latLng):
                continue

            new_cells = BoundingBox(
                minlat=latLng[0],
                minlon=latLng[1],
                maxlat=latLng[0],
                maxlon=latLng[1],
            ).get_grid_cells(expand=1)  # 3x3 grid

            way_new_cells |= new_cells - cells_set

        if way_new_cells:
            result[way_id] = tuple(way_new_cells)

    return dict(result)


def build_route_bus_stop_collections(
    elements_split: list[list[dict]],
    route_type: str,
    bbc: BoundingBoxCollection,
) -> tuple[FetchRelationBusStopCollection, ...]:
    bus_elements = elements_split[3]

    stop_area_relations = elements_split[4]
    stop_area_platform_elements = elements_split[5]
    stop_area_stop_position_elements = elements_split[6]

    merge_relations_tags(
        stop_area_relations,
        stop_area_platform_elements,
        role='platform',
        public_transport='platform',
    )
    merge_relations_tags(
        stop_area_relations,
        stop_area_stop_position_elements,
        role='stop',
        public_transport='stop_position',
    )

    elements_ex = chain(stop_area_platform_elements, stop_area_stop_position_elements, bus_elements)
    elements_ex = preprocess_elements(elements_ex)
    if route_type == 'bus':
        elements_ex = (e for e in elements_ex if is_bus_explicit(e['tags']) or not is_any_rail_related(e['tags']))
    elif route_type == 'tram':
        elements_ex = (e for e in elements_ex if is_tram_element(e['tags']))

    stops = tuple(FetchRelationBusStop.from_data(e) for e in elements_ex)
    bus_stop_collections = build_bus_stop_collections(stops)
    bus_stop_collections = tuple(c for c in bus_stop_collections if bbc.contains(c.best.latLng))
    return bus_stop_collections


# TODO: check data freshness
class Overpass:
    def __init__(self, osm: OpenStreetMap):
        self._osm = osm
        self._map_semaphore = asyncio.Semaphore(DOWNLOAD_RELATION_MAP_CONCURRENCY)

    @cached(TTLCache(maxsize=1024, ttl=7200))  # 2 hours
    async def _query_relation_history_post(
        self,
        session: str,  # cache busting  # noqa: ARG002
        query: str,
        http_timeout: float,
    ) -> list[list[dict]]:
        r = await HTTP.post(OVERPASS_API_INTERPRETER, data={'data': query}, timeout=http_timeout * 2)
        r.raise_for_status()
        elements: list[dict] = r.json()['elements']
        return split_by_count(elements)

    async def _download_map(self, bb: BoundingBox) -> list[dict]:
        try:
            async with self._map_semaphore:
                return await self._osm.get_map(bb)
        except HTTPStatusError as e:
            # the area is too large or contains too many nodes
            if e.response.status_code != status.HTTP_400_BAD_REQUEST or bb.maxlat - bb.minlat < 0.0005:
                raise

        print(f'[OSM] Splitting map download of {bb}')
        results = await asyncio.gather(*(self._download_map(half) for half in bb.split()))
        return list(chain.from_iterable(results))

    @cached(TTLCache(maxsize=1024, ttl=7200))  # 2 hours
    async def _query_relation_history_map(
        self,
        session: str,  # cache busting  # noqa: ARG002
        relation_id: int,
        cell_bbs: Sequence[BoundingBox],
        cell_bbs_expanded: Sequence[BoundingBox],
        route_type: str,
    ) -> list[list[dict]]:
        if route_type == 'bicycle':
            relation = await self._osm.get_relation(relation_id)
            relation_way_ids = frozenset(m['ref'] for m in relation['members'] if m['type'] == 'way')
        else:
            relation_way_ids = frozenset()

        results = await asyncio.gather(*(self._download_map(bb) for bb in cell_bbs_expanded))
        return build_map_elements_split(
            chain.from_iterable(results),
            relation_way_ids,
            cell_bbs,
            cell_bbs_expanded,
            route_type,
        )

    async def _query_relation_history(
        self,
        relation_id: int,
        download_hist: DownloadHistory,
        route_type: str,
    ) -> tuple[list[list[dict]], BoundingBoxCollection]:
        if not download_hist.history or not all(download_hist.history):
            raise ValueError('No grid cells to download')

        all_elements_split = None
        all_bbs = []

        for cells in download_hist.history:
            hor_bbs_t = optimize_cells_and_get_bbs(cells, start_horizontal=True)
            ver_bbs_t = optimize_cells_and_get_bbs(cells, start_horizontal=False)

            # pick more optimal cells
            cell_bbs_t = hor_bbs_t if len(hor_bbs_t) <= len(ver_bbs_t) else ver_bbs_t
            cell_bbs, cell_bbs_expand = cell_bbs_t
            all_bbs.extend(cell_bbs)

            print(f'[{DOWNLOAD_RELATION_SOURCE.upper()}] Downloading {len(cell_bbs)} cells for relation {relation_id}')

            if DOWNLOAD_RELATION_SOURCE == 'osm':
                elements_split = await self._query_relation_history_map(
                    download_hist.session, relation_id, cell_bbs, cell_bbs_expand, route_type
                )
            else:
                timeout = 180
                query = build_query(relation_id, cell_bbs, cell_bbs_expand, timeout, route_type)
                elements_split = await self._query_relation_history_post(download_hist.session, query, timeout)

            if all_elements_split is None:
                # copy, as the cached result must not be extended
                all_elements_split = [list(elements) for elements in elements_split]
            else:
                for i, elements in enumerate(elements_split):
                    all_elements_split[i].extend(elements)

        bbc = BoundingBoxCollection(all_bbs)

        return all_elements_split, bbc

    @cached(TTLCache(maxsize=128, ttl=60))
    async def query_relation(
        self,
        relation_id: int,
        download_hist: DownloadHistory | None,
        download_targets: Sequence[Cell] | None,
        route_type: str,  # bus, tram...
    ) -> tuple[
        BoundingBox,
        DownloadHistory,
        dict[ElementId, tuple[Cell, ...]],
        dict[ElementId, FetchRelationElement],
        dict[int, list[ElementId]],
        list[FetchRelationBusStopCollection],
    ]:
        if download_targets is None:
            elements = await self._osm.get_relation_full(relation_id)
            nodes_map = {e['id']: e for e in elements if e['type'] == 'node'}
            member_way_ids = {
                m['ref']
                for e in elements
                if e['type'] == 'relation' and e['id'] == relation_id
                for m in e['members']
                if m['type'] == 'way'
            }
            way_elements = [e for e in elements if e['type'] == 'way' and e['id'] in member_way_ids]
            if not way_elements:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Relation is empty, which is not supported')

            relation_way_members = {e['id'] for e in way_elements}
            relation_way_roles = {
                m['ref']: ('' if m['role'] == 'route' else m['role'])
                for e in elements
                if e['type'] == 'relation' and e['id'] == relation_id
                for m in e['members']
                if m['type'] == 'way'
            }
            union_grid_cells_set: set[Cell] = set()

            for way in way_elements:
                lats, lons = zip(*((nodes_map[n]['lat'], nodes_map[n]['lon']) for n in way['nodes'] if n in nodes_map))
                union_grid_cells_set.update(
                    BoundingBox(minlat=min(lats), minlon=min(lons), maxlat=max(lats), maxlon=max(lons))
                    .extend(DOWNLOAD_RELATION_WAY_BB_EXPAND)
                    .get_grid_cells()
                )

            union_grid_cells = tuple(union_grid_cells_set)
        else:
            # in merge mode, members (and their roles) are set by the client
            relation_way_members = set()
            relation_way_roles = {}

            union_grid_cells = download_targets

        if download_hist is None:
            download_hist = DownloadHistory(session=DownloadHistory.make_session(), history=(union_grid_cells,))
        elif union_grid_cells:
            download_hist = replace(download_hist, history=(*download_hist.history, union_grid_cells))

        elements_split, bbc = await self._query_relation_history(relation_id, download_hist, route_type)

        maybe_road_elements = elements_split[0]
        maybe_road_elements = preprocess_elements(maybe_road_elements)
        node_elements = elements_split[1]
        node_elements = preprocess_elements(node_elements)
        turn_in_place_elements = elements_split[2]
        turn_in_place_elements = preprocess_elements(turn_in_place_elements)

        # keep current members of bicycle routes regardless of tagging, e.g., dismount sections over steps
        keep_way_ids = {e['id'] for e in elements_split[3]} if route_type == 'bicycle' else set()
        road_elements = tuple(
            e for e in maybe_road_elements if e['id'] in keep_way_ids or is_routable(e['tags'], route_type)
        )

        nodes_map = {e['id']: e for e in node_elements}
        turn_in_place_nodes = {e['id'] for e in turn_in_place_elements}

        for e in road_elements:
            e['_member'] = e['id'] in relation_way_members
            e['_role'] = relation_way_roles.get(e['id'], '') if route_type == 'bicycle' else ''
            e['_oneway'] = is_oneway(e['tags'], route_type)
            e['_roundabout'] = is_roundabout(e['tags'])

        road_elements, connected_ways_map, id_map = organize_ways(road_elements, turn_in_place_nodes)

        ways = {
            e['id']: FetchRelationElement(
                id=e['id'],
                member=e['_member'],
                role=e['_role'],
                oneway=e['_oneway'],
                roundabout=e['_roundabout'],
                nodes=e['nodes'],
                latLngs=[(nodes_map[n_id]['lat'], nodes_map[n_id]['lon']) for n_id in e['nodes']],
                connectedTo=list(connected_ways_map[e['id']]),
                turn_in_place_start=e['_turn_in_place_start'],
                turn_in_place_end=e['_turn_in_place_end'],
            )
            for e in road_elements
        }

        if route_type == 'bicycle':
            bus_stop_collections = ()
        else:
            bus_stop_collections = build_route_bus_stop_collections(elements_split, route_type, bbc)

        global_bb = BoundingBox(*bbc.idx.bounds)
        download_triggers = get_download_triggers(bbc, union_grid_cells, ways)

        return global_bb, download_hist, download_triggers, ways, id_map, bus_stop_collections

    @cached(TTLCache(maxsize=128, ttl=60))
    async def query_parents(self, way_ids_set: frozenset[int]) -> QueryParentsResult:
        relations_lists = await asyncio.gather(*(self._osm.get_way_relations(way_id) for way_id in way_ids_set))
        # unique relations, the same object must be shared between ways, as it is modified in place for each split
        all_relations = tuple({r['@id']: r for r in chain.from_iterable(relations_lists)}.values())
        id_relations_map = defaultdict(list)

        for relation in all_relations:
            members = relation['member'] = relation.get('member', [])
            # tags = relation['tag'] = relation.get('tag', [])

            if len(members) <= 1:
                continue

            for member in members:
                if member['@type'] == 'way' and member['@ref'] in way_ids_set:
                    id_relations_map[member['@ref']].append(relation)

        # unique relations
        for way_id, relations in id_relations_map.items():
            id_relations_map[way_id] = list({r['@id']: r for r in relations}.values())

        # only the neighbors of the split ways are needed to determine the member order
        neighbor_way_ids = set(way_ids_set)
        for relation in all_relations:
            members = relation['member']
            for i, member in enumerate(members):
                if member['@type'] != 'way' or member['@ref'] not in way_ids_set:
                    continue
                for j in (i - 1, i + 1):
                    if 0 <= j < len(members) and members[j]['@type'] == 'way':
                        neighbor_way_ids.add(members[j]['@ref'])

        ways = await self._osm.get_ways_xml(sorted(neighbor_way_ids))
        ways_map = {w['@id']: w for w in ways}

        return QueryParentsResult(
            id_relations_map=id_relations_map,
            ways_map=ways_map,
        )
